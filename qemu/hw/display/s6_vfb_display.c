/*
 * SM-G925F virtual framebuffer scanout for QEMU's ARM64 virt board.
 *
 * The guest's Linux vfb uses vmalloc_32(), so its framebuffer has a virtual
 * address and physically scattered 4 KiB pages. The kernel notifies this
 * device when Android posts a finished frame. Snapshot that buffer through
 * the ARM64 translation tables before the guest can reuse it.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "qapi/error.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/sysbus.h"
#include "system/address-spaces.h"
#include "exec/cpu-common.h"
#include "ui/console.h"
#include "ui/surface.h"
#include "qom/object.h"

#define TYPE_S6_VFB_DISPLAY "s6-vfb-display"
OBJECT_DECLARE_SIMPLE_TYPE(S6VfbDisplayState, S6_VFB_DISPLAY)

#define S6_PAGE_OFFSET 0xffffffc000000000ULL
#define S6_RAM_BASE 0x40000000ULL
#define S6_PHYS_MASK 0x0000fffffffff000ULL

struct S6VfbDisplayState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    QemuConsole *con;
    uint32_t width, height;
    uint32_t output_width, output_height;
    uint64_t pgd_phys;
    uint64_t ptr_phys;
    uint64_t frame_base;
    uint32_t frame_line, frame_width, frame_height;
    uint8_t *pending;
    uint8_t *shadow;
    uint32_t *row_buffer;
    uint32_t *source_x;
    uint32_t *source_y;
    bool shadow_flipped;
    bool full_update;
    bool frame_ready;
    bool display_blank;
    bool landscape;
    bool have_visible_frame;
    DisplaySurface *last_surf;
    uint64_t frame_count;
    uint8_t *history;
    gint64 page_changed[4];
    bool page_seen[4];
    bool page_visible[4];
    uint32_t page_bright[4];
    gint64 last_poll;
    gint64 presented_change;
    bool follow_rendered_pages;
    char *host_frame_path;
    GMappedFile *host_frames;
    uint64_t host_sequence;
};

static uint64_t s6_rd64(hwaddr pa)
{
    uint64_t value = 0;

    address_space_read(&address_space_memory, pa, MEMTXATTRS_UNSPECIFIED,
                       &value, sizeof(value));
    return le64_to_cpu(value);
}

/* 4 KiB granule, 39-bit VA: PGD[38:30], PMD[29:21], PTE[20:12]. */
static bool s6_translate(S6VfbDisplayState *s, uint64_t va, hwaddr *pa)
{
    uint64_t entry;

    if (va >= S6_PAGE_OFFSET && va < S6_PAGE_OFFSET + 0x80000000ULL) {
        *pa = S6_RAM_BASE + (va - S6_PAGE_OFFSET);
        return true;
    }
    entry = s6_rd64(s->pgd_phys + ((va >> 30) & 0x1ff) * 8);
    if ((entry & 3) == 1) {
        *pa = (entry & S6_PHYS_MASK & ~0x3fffffffULL) |
              (va & 0x3fffffffULL);
        return true;
    }
    if ((entry & 3) != 3) {
        return false;
    }
    entry = s6_rd64((entry & S6_PHYS_MASK) + ((va >> 21) & 0x1ff) * 8);
    if ((entry & 3) == 1) {
        *pa = (entry & S6_PHYS_MASK & ~0x1fffffULL) |
              (va & 0x1fffffULL);
        return true;
    }
    if ((entry & 3) != 3) {
        return false;
    }
    entry = s6_rd64((entry & S6_PHYS_MASK) + ((va >> 12) & 0x1ff) * 8);
    if ((entry & 3) != 3) {
        return false;
    }
    *pa = (entry & S6_PHYS_MASK) | (va & 0xfff);
    return true;
}

/* Snapshot while the guest is in the framebuffer post ioctl. The buffer
 * cannot be reused by the compositor until this MMIO write returns. */
static void s6_vfb_commit(S6VfbDisplayState *s)
{
    uint32_t visible = 0;
    uint32_t *pixels = (uint32_t *)s->pending;

    if (s->follow_rendered_pages || s->host_frames) {
        s->frame_count++;
        return;
    }

    if (s->frame_width != s->width || s->frame_height != s->height ||
        s->frame_line < s->width * 4 || s->frame_line > 16384 ||
        !s->frame_base) {
        return;
    }
    s->frame_count++;
    for (uint32_t y = 0; y < s->height; y++) {
        uint64_t va = s->frame_base + (uint64_t)y * s->frame_line;
        uint32_t left = s->width * 4;
        uint8_t *dst = s->pending + (size_t)y * s->width * 4;

        while (left) {
            hwaddr pa;
            uint32_t len = MIN(left, 4096 - (va & 0xfff));

            if (!s6_translate(s, va, &pa) ||
                address_space_read(&address_space_memory, pa,
                                   MEMTXATTRS_UNSPECIFIED, dst, len) != MEMTX_OK) {
                return;
            }
            va += len;
            dst += len;
            left -= len;
        }
    }
    /* Inspect the completed snapshot, never the old/uninitialized buffer. */
    for (size_t i = 0; i < (size_t)s->width * s->height && !visible; i++) {
        visible |= pixels[i] & 0x00ffffff;
    }
    /* Mode changes can submit an empty buffer between UI frames. */
    if (!visible && s->have_visible_frame && !s->display_blank) {
        return;
    }
    s->have_visible_frame |= visible != 0;
    uint8_t *previous = s->shadow;
    s->shadow = s->pending;
    s->pending = previous;
    s->shadow_flipped = false;
    s->frame_ready = true;
    s->full_update = true;
}

static uint64_t s6_vfb_read(void *opaque, hwaddr offset, unsigned size)
{
    S6VfbDisplayState *s = opaque;

    switch (offset) {
    case 0: return 0x53365646 | (size == 8 ? s->frame_count << 32 : 0);
    case 4: return s->frame_count;
    case 8: return s->frame_base;
    case 12: return s->frame_base >> 32;
    case 16: return s->frame_line | (size == 8 ? (uint64_t)s->frame_width << 32 : 0);
    case 20: return s->frame_width;
    case 24: return s->frame_height;
    case 32: return s->display_blank;
    case 36: return 0; /* Reserved; formerly an empty-frame debug counter. */
    case 40: return s->landscape;
    default: return 0;
    }
}

static void s6_vfb_write(void *opaque, hwaddr offset, uint64_t value,
                         unsigned size)
{
    S6VfbDisplayState *s = opaque;

    switch (offset) {
    case 8:
        if (size == 8) {
            s->frame_base = value;
        } else {
            s->frame_base = (s->frame_base & 0xffffffff00000000ULL) | value;
        }
        break;
    case 12:
        s->frame_base = (s->frame_base & 0xffffffffULL) | (value << 32);
        break;
    case 16: s->frame_line = value; break;
    case 20: s->frame_width = value; break;
    case 24: s->frame_height = value; break;
    case 28: s6_vfb_commit(s); break;
    case 32:
        s->display_blank = value != 0;
        s->full_update = true;
        break;
    }
}

static const MemoryRegionOps s6_vfb_mmio_ops = {
    .read = s6_vfb_read,
    .write = s6_vfb_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 8 },
    .impl = { .min_access_size = 4, .max_access_size = 8 },
};

/* Samsung's legacy software path posts page zero while rendering into the
 * other slots. Present a nonempty slot only after two unchanged polls, so
 * neither its clear nor its partially painted contents reach the window. */
static void s6_vfb_poll_rendered(S6VfbDisplayState *s)
{
    gint64 now = g_get_monotonic_time();
    uint64_t base = s6_rd64(s->ptr_phys);
    size_t bytes = (size_t)s->width * s->height * 4;
    int selected = -1;
    gint64 newest = s->presented_change;

    if (!base || now - s->last_poll < 33000) {
        return;
    }
    s->last_poll = now;
    for (int page = 0; page < 4; page++) {
        bool valid = true;
        bool changed = !s->page_seen[page];
        uint8_t *previous = s->history + page * bytes;

        for (size_t off = 0; off < bytes;) {
            uint64_t va = base + page * bytes + off;
            size_t len = MIN(bytes - off, 4096 - (va & 0xfff));
            hwaddr pa;

            if (!s6_translate(s, va, &pa) ||
                address_space_read(&address_space_memory, pa,
                    MEMTXATTRS_UNSPECIFIED, s->pending + off, len) != MEMTX_OK) {
                valid = false;
                break;
            }
            off += len;
        }
        if (!valid) {
            continue;
        }
        if (!changed) {
            uint32_t *old = (uint32_t *)previous;
            uint32_t *current = (uint32_t *)s->pending;

            for (size_t i = 0; i < bytes / 4; i++) {
                if ((old[i] ^ current[i]) & 0x00ffffff) {
                    changed = true;
                    break;
                }
            }
        }
        if (changed) {
            uint32_t bright = 0;
            uint32_t *pixels = (uint32_t *)s->pending;

            memcpy(previous, s->pending, bytes);
            for (size_t i = 0; i < bytes / 4; i++) {
                uint32_t pixel = pixels[i];

                bright += (pixel & 0xff) > 16 ||
                          ((pixel >> 8) & 0xff) > 16 ||
                          ((pixel >> 16) & 0xff) > 16;
            }
            s->page_seen[page] = true;
            /* Cleared slots can retain tiny glyphs or near-black pixels. */
            s->page_visible[page] = bright >= MAX(8, bytes / 4 / 200);
            s->page_bright[page] = bright;
            s->page_changed[page] = now;
        } else if (s->page_visible[page] &&
                   now - s->page_changed[page] >= 66000 &&
                   s->page_changed[page] > newest) {
            selected = page;
            newest = s->page_changed[page];
        }
    }
    if (selected < 0 && now - s->presented_change > 200000) {
        /* Keep progress when both software slots are being updated rapidly.
         * Prefer the filled slot, never a cleared or near-black one. */
        uint32_t most = 0;

        for (int page = 0; page < 4; page++) {
            if (s->page_visible[page] && s->page_changed[page] > newest &&
                s->page_bright[page] >= most) {
                selected = page;
                newest = s->page_changed[page];
                most = s->page_bright[page];
            }
        }
    }
    if (selected >= 0) {
        memcpy(s->shadow, s->history + selected * bytes, bytes);
        s->shadow_flipped = false;
        s->presented_change = newest;
        s->frame_ready = true;
        s->have_visible_frame = true;
        s->full_update = true;
    }
}

static void s6_vfb_poll_host(S6VfbDisplayState *s)
{
    const uint8_t *file = (const uint8_t *)g_mapped_file_get_contents(s->host_frames);
    uint64_t sequence, after;
    int32_t direction;
    size_t bytes = (size_t)s->width * s->height * 4;

    sequence = qatomic_read((const uint64_t *)(file + 8));
    sequence = le64_to_cpu(sequence);
    if (!sequence || (sequence & 1) || sequence == s->host_sequence) {
        return;
    }
    smp_rmb();
    memcpy(&direction, file + 24, sizeof(direction));
    direction = le32_to_cpu(direction);
    memcpy(s->pending, file + 4096, bytes);
    smp_rmb();
    after = qatomic_read((const uint64_t *)(file + 8));
    if (le64_to_cpu(after) != sequence) {
        return;
    }
    /* Keep the stable snapshot and flip during conversion. Swapping avoids
     * copying another full 720x1280 frame for every renderer callback. */
    uint8_t *previous = s->shadow;
    s->shadow = s->pending;
    s->pending = previous;
    s->shadow_flipped = direction > 0;
    s->host_sequence = sequence;
    s->frame_ready = true;
    s->full_update = true;
}

static bool s6_vfb_update(void *opaque)
{
    S6VfbDisplayState *s = opaque;
    DisplaySurface *surf = qemu_console_surface(s->con);
    uint8_t *dst;
    int stride;
    uint32_t width = s->landscape ? s->output_height : s->output_width;
    uint32_t height = s->landscape ? s->output_width : s->output_height;
    uint32_t first_changed = height, last_changed = 0;

    if (!s->display_blank) {
        if (s->host_frames) {
            s6_vfb_poll_host(s);
        } else if (s->follow_rendered_pages) {
            s6_vfb_poll_rendered(s);
        }
    }

    if (!surf || (!s->frame_ready && !s->display_blank)) {
        return true;
    }
    if (surf != s->last_surf) {
        s->last_surf = surf;
        s->full_update = true;
    }
    if (!s->full_update) {
        return true;
    }
    dst = surface_data(surf);
    stride = surface_stride(surf);
    for (uint32_t y = 0; y < height; y++) {
        size_t row_bytes = width * 4;
        uint8_t *row = dst + (size_t)y * stride;

        if (s->display_blank) {
            memset(s->row_buffer, 0, row_bytes);
        } else if (s->landscape) {
            uint32_t src_x = s->source_x[s->output_width - 1 - y];

            for (uint32_t x = 0; x < width; x++) {
                uint32_t src_y = s->source_y[x];

                if (s->shadow_flipped) {
                    src_y = s->height - 1 - src_y;
                }
                const uint8_t *pixel = s->shadow +
                    (size_t)src_y * s->width * 4 + src_x;

                s->row_buffer[x] = (pixel[0] << 16) |
                                   (pixel[1] << 8) | pixel[2];
            }
        } else {
            uint32_t src_y = s->source_y[y];

            if (s->shadow_flipped) {
                src_y = s->height - 1 - src_y;
            }
            const uint8_t *source = s->shadow + (size_t)src_y * s->width * 4;

            for (uint32_t x = 0; x < width; x++) {
                const uint8_t *pixel = source + s->source_x[x];

                s->row_buffer[x] = (pixel[0] << 16) |
                                   (pixel[1] << 8) | pixel[2];
            }
        }
        if (memcmp(row, s->row_buffer, row_bytes)) {
            memcpy(row, s->row_buffer, row_bytes);
            first_changed = MIN(first_changed, y);
            last_changed = y;
        }
    }
    s->full_update = false;
    if (first_changed < height) {
        qemu_console_update(s->con, 0, first_changed, width,
                            last_changed - first_changed + 1);
    }
    return true;
}

static void s6_vfb_invalidate(void *opaque)
{
    S6VfbDisplayState *s = opaque;

    s->full_update = true;
}

static const GraphicHwOps s6_vfb_ops = {
    .invalidate = s6_vfb_invalidate,
    .gfx_update = s6_vfb_update,
};

static bool s6_vfb_get_landscape(Object *obj, Error **errp)
{
    return S6_VFB_DISPLAY(obj)->landscape;
}

static void s6_vfb_set_landscape(Object *obj, bool landscape, Error **errp)
{
    S6VfbDisplayState *s = S6_VFB_DISPLAY(obj);

    if (s->landscape == landscape) {
        return;
    }
    s->landscape = landscape;
    s->full_update = true;
    if (s->con) {
        qemu_console_resize(s->con,
                            landscape ? s->output_height : s->output_width,
                            landscape ? s->output_width : s->output_height);
    }
}

static void s6_vfb_realize(DeviceState *dev, Error **errp)
{
    S6VfbDisplayState *s = S6_VFB_DISPLAY(dev);

    if (!s->width || !s->height || s->width > 16384 || s->height > 16384 ||
        !s->output_width || !s->output_height ||
        s->output_width > s->width || s->output_height > s->height) {
        error_setg(errp, "S6 framebuffer output must fit inside the guest screen");
        return;
    }

    s->pending = g_malloc((size_t)s->width * s->height * 4);
    memory_region_init_io(&s->mmio, OBJECT(s), &s6_vfb_mmio_ops, s,
                          "s6-vfb-post", 0x1000);
    sysbus_init_mmio(SYS_BUS_DEVICE(s), &s->mmio);
    sysbus_mmio_map(SYS_BUS_DEVICE(s), 0, 0x090f0000);
    s->shadow = g_malloc0((size_t)s->width * s->height * 4);
    if (s->host_frame_path) {
        GError *error = NULL;
        size_t required = 4096 + (size_t)s->width * s->height * 4;

        s->host_frames = g_mapped_file_new(s->host_frame_path, false, &error);
        if (!s->host_frames || g_mapped_file_get_length(s->host_frames) < required) {
            error_setg(errp, "Cannot map GPU frame file: %s",
                       error ? error->message : "file too small");
            g_clear_error(&error);
            return;
        }
        const uint8_t *header = (const uint8_t *)g_mapped_file_get_contents(s->host_frames);
        if (memcmp(header, "G925GPU1", 8) ||
            ldl_le_p(header + 16) != s->width ||
            ldl_le_p(header + 20) != s->height ||
            ldl_le_p(header + 28) != 0x1908 ||
            ldl_le_p(header + 32) != 0x1401) {
            error_setg(errp, "GPU frame file must contain matching RGBA8 frames");
            return;
        }
    } else if (s->follow_rendered_pages) {
        s->history = g_malloc0((size_t)s->width * s->height * 4 * 4);
    }
    s->row_buffer = g_new(uint32_t, MAX(s->output_width, s->output_height));
    s->source_x = g_new(uint32_t, s->output_width);
    s->source_y = g_new(uint32_t, s->output_height);
    for (uint32_t x = 0; x < s->output_width; x++) {
        s->source_x[x] = ((uint64_t)x * s->width / s->output_width) * 4;
    }
    for (uint32_t y = 0; y < s->output_height; y++) {
        s->source_y[y] = (uint64_t)y * s->height / s->output_height;
    }
    s->full_update = true;
    s->con = qemu_graphic_console_create(dev, 0, &s6_vfb_ops, s);
    qemu_console_resize(s->con,
                        s->landscape ? s->output_height : s->output_width,
                        s->landscape ? s->output_width : s->output_height);
}

static const Property s6_vfb_properties[] = {
    DEFINE_PROP_UINT32("width", S6VfbDisplayState, width, 720),
    DEFINE_PROP_UINT32("height", S6VfbDisplayState, height, 1280),
    DEFINE_PROP_UINT32("output-width", S6VfbDisplayState, output_width, 360),
    DEFINE_PROP_UINT32("output-height", S6VfbDisplayState, output_height, 640),
    DEFINE_PROP_UINT64("pgd-phys", S6VfbDisplayState, pgd_phys, 0x4007d000),
    DEFINE_PROP_UINT64("ptr-phys", S6VfbDisplayState, ptr_phys, 0x40811548),
    DEFINE_PROP_BOOL("follow-rendered-pages", S6VfbDisplayState,
                     follow_rendered_pages, true),
    DEFINE_PROP_STRING("host-frame-path", S6VfbDisplayState, host_frame_path),
};

static void s6_vfb_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);

    dc->desc = "SM-G925F ARM64 virtual framebuffer scanout";
    dc->realize = s6_vfb_realize;
    device_class_set_props(dc, s6_vfb_properties);
    object_class_property_add_bool(oc, "landscape",
                                   s6_vfb_get_landscape, s6_vfb_set_landscape);
}

static const TypeInfo s6_vfb_types[] = {
    {
        .name = TYPE_S6_VFB_DISPLAY,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(S6VfbDisplayState),
        .class_init = s6_vfb_class_init,
    },
};

DEFINE_TYPES(s6_vfb_types)
