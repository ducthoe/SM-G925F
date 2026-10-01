/* F7 rotates Android to match the virtual phone's physical orientation. */

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/system_properties.h>
#include <unistd.h>

#define DISPLAY_MMIO 0x090f0000
#define ORIENTATION_OFFSET 40

static int open_keyboard(void)
{
    DIR *devices = opendir("/dev/input");
    struct dirent *entry;
    int keyboard = -1;

    if (!devices) {
        return -1;
    }
    while ((entry = readdir(devices))) {
        char path[512], name[128] = {0};

        if (strncmp(entry->d_name, "event", 5)) {
            continue;
        }
        snprintf(path, sizeof(path), "/dev/input/%s", entry->d_name);
        int fd = open(path, O_RDONLY | O_CLOEXEC);

        if (fd < 0) {
            continue;
        }
        if (ioctl(fd, EVIOCGNAME(sizeof(name)), name) >= 0 &&
            !strcmp(name, "QEMU Virtio Keyboard")) {
            keyboard = fd;
            break;
        }
        close(fd);
    }
    closedir(devices);
    return keyboard;
}

static int apply_rotation(const volatile uint32_t *orientation)
{
    char command[256];
    unsigned int rotation = *orientation ? 1 : 0;

    snprintf(command, sizeof(command),
             "/system/bin/sh /system/bin/settings put system accelerometer_rotation 0 && "
             "/system/bin/sh /system/bin/settings put system user_rotation %u", rotation);
    return system(command);
}

int main(void)
{
    char boot[PROP_VALUE_MAX];
    int log = open("/data/g925-rotation.log", O_WRONLY | O_CREAT | O_APPEND, 0600);

    if (log >= 0) {
        dup2(log, STDOUT_FILENO);
        dup2(log, STDERR_FILENO);
        close(log);
    }
    while (__system_property_get("sys.boot_completed", boot) <= 0 || strcmp(boot, "1")) {
        sleep(1);
    }
    int memory = open("/dev/mem", O_RDONLY | O_CLOEXEC);
    void *registers = memory < 0 ? MAP_FAILED :
        mmap(NULL, 4096, PROT_READ, MAP_SHARED, memory, DISPLAY_MMIO);

    if (registers == MAP_FAILED) {
        perror("Cannot read display orientation");
        return 1;
    }
    close(memory);
    const volatile uint32_t *orientation =
        (const volatile uint32_t *)((const char *)registers + ORIENTATION_OFFSET);
    int keyboard = open_keyboard();

    if (keyboard < 0 || apply_rotation(orientation)) {
        fprintf(stderr, "Cannot initialize rotation controls\n");
        return 1;
    }
    struct input_event event;

    for (;;) {
        ssize_t count = read(keyboard, &event, sizeof(event));

        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count != sizeof(event)) {
            fprintf(stderr, "Keyboard event stream closed\n");
            return 1;
        }
        if (event.type == EV_KEY && event.code == KEY_F7 && event.value == 1 &&
            apply_rotation(orientation)) {
            fprintf(stderr, "Cannot change Android orientation\n");
        }
    }
}
