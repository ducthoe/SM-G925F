/* Goldfish v1 pipe transport for Android 5 GLES host rendering.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/sysbus.h"
#include "qemu/main-loop.h"
#include "hw/core/irq.h"
#include "qemu/sockets.h"
#include "system/address-spaces.h"
#include "migration/vmstate.h"
#include "qom/object.h"
#include <poll.h>

#define TYPE_G925_PIPE "g925-goldfish-pipe"
OBJECT_DECLARE_SIMPLE_TYPE(G925PipeState, G925_PIPE)
typedef struct G925Channel G925Channel;
struct G925PipeState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq irq;
    GHashTable *channels;
    GQueue wakes;
    G925Channel *active_wake;
    uint32_t channel, size, port;
    uint64_t address, params;
    int32_t status;
};
struct G925Channel {
    G925PipeState *dev;
    uint32_t id, wake_flags;
    int fd;
    bool want_read, want_write, queued, process_pipe;
    uint64_t process_id;
    uint32_t process_offset;
    GByteArray *name;
};

static void update_irq(G925PipeState *s)
{
    qemu_set_irq(s->irq, s->active_wake || !g_queue_is_empty(&s->wakes));
}
static void watch_channel(G925Channel *p);
static void notify_channel(G925Channel *p, uint32_t flags)
{
    p->wake_flags |= flags;
    if (!p->queued) {
        p->queued = true;
        g_queue_push_tail(&p->dev->wakes, p);
    }
    update_irq(p->dev);
}
static void channel_readable(void *opaque)
{
    G925Channel *p = opaque;
    char byte;
    int result = recv(p->fd, &byte, 1, MSG_PEEK | MSG_DONTWAIT);
    p->want_read = false;
    watch_channel(p);
    notify_channel(p, result == 0 ? 7 : 2);
}
static void channel_writable(void *opaque)
{
    G925Channel *p = opaque;
    p->want_write = false;
    watch_channel(p);
    notify_channel(p, 4);
}
static void watch_channel(G925Channel *p)
{
    if (p->fd >= 0) {
        qemu_set_fd_handler(p->fd, p->want_read ? channel_readable : NULL,
                            p->want_write ? channel_writable : NULL, p);
    }
}
static void close_channel(G925Channel *p)
{
    G925PipeState *s = p->dev;
    if (p->fd >= 0) {
        qemu_set_fd_handler(p->fd, NULL, NULL, NULL);
        close(p->fd);
    }
    if (p->queued) {
        g_queue_remove(&s->wakes, p);
    }
    if (s->active_wake == p) {
        s->active_wake = NULL;
    }
    g_byte_array_unref(p->name);
    g_free(p);
    update_irq(s);
}
static int connect_channel(G925Channel *p)
{
    struct sockaddr_in address = {
        .sin_family = AF_INET,
        .sin_port = htons(p->dev->port),
        .sin_addr.s_addr = htonl(INADDR_LOOPBACK),
    };
    p->fd = qemu_socket(AF_INET, SOCK_STREAM, 0);
    if (p->fd < 0 || connect(p->fd, (struct sockaddr *)&address,
                             sizeof(address)) < 0) {
        if (p->fd >= 0) {
            close(p->fd);
        }
        p->fd = -1;
        return -4;
    }
    /* GLES commands often wait for a small synchronous reply. Do not
     * delay the tail of a command behind TCP's Nagle algorithm. */
    socket_set_nodelay(p->fd);
    fcntl(p->fd, F_SETFL, fcntl(p->fd, F_GETFL) | O_NONBLOCK);
    return 0;
}
static int write_channel(G925Channel *p, const uint8_t *data, uint32_t size)
{
    uint32_t consumed = 0;
    int result;
    if (p->fd < 0 && !p->process_pipe) {
        while (consumed < size) {
            uint8_t value = data[consumed++];
            g_byte_array_append(p->name, &value, 1);
            if (p->name->len > 256) {
                return -1;
            }
            if (!value) {
                const char *name = (const char *)p->name->data;
                if (!strcmp(name, "pipe:GLProcessPipe")) {
                    /* SDK 24 has no process-ID protocol. The guest driver
                     * explicitly supports falling back when unavailable. */
                    return -1;
                }
                if (strncmp(name, "pipe:opengles", 13)) {
                    return -1;
                }
                if (connect_channel(p) < 0) {
                    return -4;
                }
                break;
            }
        }
        if (p->fd < 0 && !p->process_pipe) {
            return consumed;
        }
    }
    if (p->process_pipe) {
        return size;
    }
    if (consumed == size) {
        return consumed;
    }
    result = send(p->fd, data + consumed, size - consumed, MSG_NOSIGNAL);
    if (result < 0) {
        return consumed ? consumed : (errno == EAGAIN ? -2 : -4);
    }
    return consumed + result;
}
static int pipe_command(G925PipeState *s, uint32_t channel, uint32_t command,
                        uint64_t address, uint32_t size)
{
    G925Channel *p = g_hash_table_lookup(s->channels, GUINT_TO_POINTER(channel));
    uint8_t *data;
    hwaddr mapped;
    int result;
    if (command == 1) {
        if (p || !channel) {
            return -1;
        }
        p = g_new0(G925Channel, 1);
        p->dev = s;
        p->id = channel;
        p->fd = -1;
        p->name = g_byte_array_new();
        g_hash_table_insert(s->channels, GUINT_TO_POINTER(channel), p);
        return 0;
    }
    if (!p) {
        return -1;
    }
    switch (command) {
    case 2:
        g_hash_table_remove(s->channels, GUINT_TO_POINTER(channel));
        close_channel(p);
        return 0;
    case 3: {
        struct pollfd fd = { .fd = p->fd, .events = POLLIN | POLLOUT };
        if (p->process_pipe) {
            return 3;
        }
        if (p->fd < 0) {
            return 2;
        }
        poll(&fd, 1, 0);
        return ((fd.revents & POLLIN) ? 1 : 0) |
               ((fd.revents & POLLOUT) ? 2 : 0) |
               ((fd.revents & (POLLHUP | POLLERR)) ? 4 : 0);
    }
    case 5:
        p->want_write = true;
        if (p->process_pipe) {
            notify_channel(p, 4);
        } else {
            watch_channel(p);
        }
        return 0;
    case 7:
        p->want_read = true;
        if (p->process_pipe) {
            notify_channel(p, 2);
        } else {
            watch_channel(p);
        }
        return 0;
    case 4:
    case 6:
        if (!size || size > 16 * 1024 * 1024) {
            return -1;
        }
        if (command == 6 && p->fd < 0 && !p->process_pipe) {
            return -1;
        }
        /* The guest supplies a physically contiguous bounce buffer.
         * Map it for this command only: socket I/O can use RAM directly,
         * and unmap dirties only bytes actually received. A short mapping
         * is a normal partial transfer, just like a short socket write. */
        mapped = size;
        if (!address_space_access_valid(&address_space_memory, address, size,
                                        command == 6,
                                        MEMTXATTRS_UNSPECIFIED)) {
            return -1;
        }
        data = address_space_map(&address_space_memory, address, &mapped,
                                 command == 6, MEMTXATTRS_UNSPECIFIED);
        if (!data) {
            return -2;
        }
        if (command == 4) {
            result = write_channel(p, data, mapped);
        } else if (p->process_pipe) {
            uint64_t id = cpu_to_le64(p->process_id);
            result = MIN(mapped, 8 - MIN(p->process_offset, 8));
            memcpy(data, (uint8_t *)&id + p->process_offset, result);
            p->process_offset += result;
        } else {
            result = recv(p->fd, data, mapped, MSG_DONTWAIT);
            if (result < 0) {
                result = errno == EAGAIN ? -2 : -4;
            }
        }
        address_space_unmap(&address_space_memory, data, mapped, command == 6,
                            result > 0 ? result : 0);
        return result;
    default:
        return -1;
    }
}
static uint64_t pipe_read(void *opaque, hwaddr offset, unsigned size)
{
    G925PipeState *s = opaque;
    switch (offset) {
    case 4: return (uint32_t)s->status;
    case 8:
        if (!s->active_wake) {
            s->active_wake = g_queue_pop_head(&s->wakes);
        }
        update_irq(s);
        return s->active_wake ? s->active_wake->id : 0;
    case 0x14:
        if (s->active_wake) {
            G925Channel *p = s->active_wake;
            uint32_t flags = p->wake_flags;
            p->wake_flags = 0;
            p->queued = false;
            s->active_wake = NULL;
            update_irq(s);
            return flags;
        }
        return 0;
    case 0x18: return s->params;
    case 0x1c: return s->params >> 32;
    case 0x24: return s->address >> 32;
    default: return 0;
    }
}
static void pipe_write(void *opaque, hwaddr offset, uint64_t value, unsigned size)
{
    G925PipeState *s = opaque;
    switch (offset) {
    case 0:
        s->status = pipe_command(s, s->channel, value, s->address, s->size);
        break;
    case 8: s->channel = value; break;
    case 0xc: s->size = value; break;
    case 0x10: s->address = (s->address & 0xffffffff00000000ULL) | value; break;
    case 0x18: s->params = (s->params & 0xffffffff00000000ULL) | value; break;
    case 0x1c: s->params = (s->params & 0xffffffffULL) | (value << 32); break;
    case 0x24: s->address = (s->address & 0xffffffffULL) | (value << 32); break;
    case 0x20: {
        uint32_t params[6];
        uint32_t result;
        address_space_read(&address_space_memory, s->params,
                           MEMTXATTRS_UNSPECIFIED, params, sizeof(params));
        result = cpu_to_le32(pipe_command(s, le32_to_cpu(params[0]),
                             le32_to_cpu(params[3]),
                             ((uint64_t)le32_to_cpu(params[5]) << 32) |
                             le32_to_cpu(params[2]),
                             le32_to_cpu(params[1])));
        address_space_write(&address_space_memory, s->params + 16,
                            MEMTXATTRS_UNSPECIFIED, &result, sizeof(result));
        break;
    }
    }
}
static const MemoryRegionOps pipe_ops = {
    .read = pipe_read, .write = pipe_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
    .impl = { .min_access_size = 4, .max_access_size = 4 },
};
static void pipe_realize(DeviceState *dev, Error **errp)
{
    G925PipeState *s = G925_PIPE(dev);
    s->channels = g_hash_table_new(g_direct_hash, g_direct_equal);
    g_queue_init(&s->wakes);
    memory_region_init_io(&s->mmio, OBJECT(s), &pipe_ops, s, "g925-pipe", 0x1000);
    sysbus_init_mmio(SYS_BUS_DEVICE(s), &s->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(s), &s->irq);
}
static const VMStateDescription pipe_vmstate = {
    .name = "g925-goldfish-pipe", .unmigratable = true,
};
static const Property pipe_properties[] = {
    DEFINE_PROP_UINT32("port", G925PipeState, port, 22468),
};
static void pipe_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->desc = "Android 5 Goldfish GLES stream transport";
    dc->realize = pipe_realize;
    dc->vmsd = &pipe_vmstate;
    device_class_set_props(dc, pipe_properties);
}
static const TypeInfo pipe_type = {
    .name = TYPE_G925_PIPE, .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(G925PipeState), .class_init = pipe_class_init,
};
static void register_pipe_type(void) { type_register_static(&pipe_type); }
type_init(register_pipe_type)
