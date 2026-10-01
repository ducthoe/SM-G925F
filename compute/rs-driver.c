/* Small forwarding driver for the supported Samsung Android 5 RS HAL.
 * Keep the stock CPU driver and replace only supported intrinsic dispatches.
 */
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <android/log.h>

#ifdef __LP64__
#define LIBDIR "/system/lib64"
#define SIZE_CODE "m"
#else
#define LIBDIR "/system/lib"
#define SIZE_CODE "j"
#endif

#define CONTEXT "PKN7android12renderscript7ContextE"
#define SCRIPT "PKNS0_6ScriptE"
#define MAX_IMAGE (32U * 1024U * 1024U)
#define HAL_POINTERS 86

typedef struct Entry Entry;
struct Entry {
    const void *script;
    const void *input;
    unsigned channels;
    float radius;
    Entry *next;
};

typedef struct {
    uint8_t *data;
    unsigned width, height, channels;
    size_t pitch, length;
} Image;

static Entry *entries;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_once_t once = PTHREAD_ONCE_INIT;
static int pipe_fd = -1;
static bool pipe_checked, hooks_ready;
static bool (*stock_init)(void *, unsigned, unsigned);
static bool (*stock_intrinsic)(const void *, void *, unsigned, void *);
static void (*stock_var)(const void *, const void *, unsigned, void *, size_t);
static void (*stock_object)(const void *, const void *, unsigned, void *);
static void (*stock_foreach)(const void *, void *, unsigned, const void *, void *, const void *, size_t, const void *);
static void (*stock_destroy)(const void *, void *);
static uint8_t *(*offset_pointer)(const void *, unsigned, unsigned, unsigned, unsigned, unsigned);
static void *(*allocation_type)(const void *, const void *);
static void (*type_info)(const void *, const void *, uintptr_t *, unsigned);
static void (*element_info)(const void *, const void *, unsigned *, unsigned);
static void (*release_user_ref)(const void *);

static void load_stock(void)
{
    void *stock = dlopen(LIBDIR "/libRSDriver.stock.so", RTLD_NOW | RTLD_LOCAL);
    void *rs = dlopen("libRS.so", RTLD_NOW | RTLD_LOCAL);
    if (!stock || !rs) {
        __android_log_print(ANDROID_LOG_ERROR, "G925RS", "Cannot load stock RS driver: %s", dlerror());
        return;
    }
    stock_init = dlsym(stock, "rsdHalInit");
    stock_intrinsic = dlsym(stock, "_Z16rsdInitIntrinsic" CONTEXT "PNS0_6ScriptE19RsScriptIntrinsicIDPNS0_7ElementE");
    stock_var = dlsym(stock, "_Z21rsdScriptSetGlobalVar" CONTEXT SCRIPT "jPv" SIZE_CODE);
    stock_object = dlsym(stock, "_Z21rsdScriptSetGlobalObj" CONTEXT SCRIPT "jPNS0_10ObjectBaseE");
    stock_foreach = dlsym(stock, "_Z22rsdScriptInvokeForEach" CONTEXT "PNS0_6ScriptEjPKNS0_10AllocationEPS6_PKv" SIZE_CODE "PK12RsScriptCall");
    stock_destroy = dlsym(stock, "_Z16rsdScriptDestroy" CONTEXT "PNS0_6ScriptE");
    offset_pointer = dlsym(stock, "_Z12GetOffsetPtrPKN7android12renderscript10AllocationEjjjj23RsAllocationCubemapFace");
    allocation_type = dlsym(rs, "rsaAllocationGetType");
    type_info = dlsym(rs, "rsaTypeGetNativeData");
    element_info = dlsym(rs, "rsaElementGetNativeData");
    release_user_ref = dlsym(rs, "_ZNK7android12renderscript10ObjectBase10decUserRefEv");
    hooks_ready = stock_intrinsic && stock_var && stock_object && stock_foreach &&
                  stock_destroy && offset_pointer && allocation_type &&
                  type_info && element_info && release_user_ref;
}

static Entry *find_entry(const void *script)
{
    for (Entry *entry = entries; entry; entry = entry->next) {
        if (entry->script == script) {
            return entry;
        }
    }
    return NULL;
}

static bool transfer(void *data, size_t size, bool writing)
{
    uint8_t *bytes = data;
    while (size) {
        ssize_t count = writing ? write(pipe_fd, bytes, size) : read(pipe_fd, bytes, size);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            return false;
        }
        bytes += count;
        size -= count;
    }
    return true;
}

static void disconnect_pipe(void)
{
    if (pipe_fd >= 0) {
        close(pipe_fd);
        pipe_fd = -1;
    }
}

static bool connect_pipe(void)
{
    if (pipe_fd >= 0) {
        return true;
    }
    if (pipe_checked) {
        return false;
    }
    pipe_checked = true;
    /* The Android 5 pipe driver arms wakeups in blocking read/write. Its
     * poll callback only checks readiness and does not arm those wakeups. */
    pipe_fd = open("/dev/goldfish_pipe", O_RDWR | O_CLOEXEC);
    if (pipe_fd < 0) {
        return false;
    }
    char name[] = "pipe:g925-renderscript";
    uint32_t request[2] = {0, 0}, response[2], capabilities[2];
    if (transfer(name, sizeof(name), true) && transfer(request, sizeof(request), true) &&
        transfer(response, sizeof(response), false) && response[0] == 0 &&
        response[1] == sizeof(capabilities) && transfer(capabilities, sizeof(capabilities), false) &&
        capabilities[0] == 1 && (capabilities[1] & (1U << 5))) {
        return true;
    }
    disconnect_pipe();
    return false;
}

static bool get_image(const void *context, const void *allocation, Image *image)
{
    uintptr_t dimensions[6] = {0};
    unsigned element[5];
    if (!allocation) {
        return false;
    }
    void *type = allocation_type(context, allocation);
    if (!type) {
        return false;
    }
    type_info(context, type, dimensions, 6);
    release_user_ref(type);
    if (!dimensions[5]) {
        return false;
    }
    element_info(context, (const void *)dimensions[5], element, 5);
    release_user_ref((const void *)dimensions[5]);
    if (!dimensions[0] || dimensions[0] > 16384 || dimensions[1] > 16384 ||
        dimensions[2] || dimensions[3] || dimensions[4] || element[0] != 8 ||
        (element[3] != 1 && element[3] != 4) || element[4]) {
        return false;
    }
    image->width = dimensions[0];
    image->height = dimensions[1] ? dimensions[1] : 1;
    image->channels = element[3];
    image->data = offset_pointer(allocation, 0, 0, 0, 0, 0);
    uintptr_t next_row = (uintptr_t)offset_pointer(allocation, 0, 1, 0, 0, 0);
    if (!image->data || next_row <= (uintptr_t)image->data) {
        return false;
    }
    image->pitch = next_row - (uintptr_t)image->data;
    uint64_t row = (uint64_t)image->width * image->channels;
    uint64_t length = (uint64_t)image->pitch * (image->height - 1) + row;
    if (image->pitch < row || length > 64U * 1024U * 1024U ||
        row * image->height > MAX_IMAGE) {
        return false;
    }
    image->length = length;
    return true;
}

static bool gpu_blur(const void *context, Entry *entry, void *output)
{
    Image input, destination;
    if (entry->input == output || !isfinite(entry->radius) ||
        entry->radius <= 0 || entry->radius > 25 ||
        !get_image(context, entry->input, &input) ||
        !get_image(context, output, &destination) ||
        input.width != destination.width || input.height != destination.height ||
        input.channels != entry->channels || input.channels != destination.channels ||
        !connect_pipe()) {
        return false;
    }
    size_t row = input.width * input.channels, size = row * input.height;
    uint8_t *pixels = malloc(size);
    if (!pixels) {
        return false;
    }
    struct {
        uint32_t command, length, width, height, channels, pitch, input_length;
        float radius;
    } request = {5, 24 + input.length, input.width, input.height,
                 input.channels, input.pitch, input.length, entry->radius};
    uint32_t response[2];
    bool success = transfer(&request, sizeof(request), true) &&
                   transfer(input.data, input.length, true) &&
                   transfer(response, sizeof(response), false) &&
                   response[0] == 0 && response[1] == size &&
                   transfer(pixels, size, false);
    if (success) {
        /* Apply a completed reply only; failures leave the CPU inputs intact. */
        for (unsigned y = 0; y < input.height; y++) {
            memcpy(destination.data + y * destination.pitch, pixels + y * row, row);
        }
    } else {
        disconnect_pipe();
    }
    free(pixels);
    return success;
}

static bool init_intrinsic(const void *context, void *script, unsigned id, void *element)
{
    if (!stock_intrinsic(context, script, id, element)) {
        return false;
    }
    if (id == 5) {
        unsigned info[5];
        element_info(context, element, info, 5);
        if (info[0] == 8 && (info[3] == 1 || info[3] == 4) && !info[4]) {
            Entry *entry = calloc(1, sizeof(*entry));
            if (entry) {
                entry->script = script;
                entry->channels = info[3];
                entry->radius = 5;
                pthread_mutex_lock(&lock);
                entry->next = entries;
                entries = entry;
                pthread_mutex_unlock(&lock);
            }
        }
    }
    return true;
}

static void set_var(const void *context, const void *script, unsigned slot, void *data, size_t length)
{
    stock_var(context, script, slot, data, length);
    pthread_mutex_lock(&lock);
    Entry *entry = find_entry(script);
    if (entry && slot == 0 && length == sizeof(float)) {
        memcpy(&entry->radius, data, sizeof(float));
    }
    pthread_mutex_unlock(&lock);
}

static void set_object(const void *context, const void *script, unsigned slot, void *object)
{
    stock_object(context, script, slot, object);
    pthread_mutex_lock(&lock);
    Entry *entry = find_entry(script);
    if (entry && slot == 1) {
        entry->input = object;
    }
    pthread_mutex_unlock(&lock);
}

static void invoke_foreach(const void *context, void *script, unsigned slot,
                           const void *input, void *output, const void *user,
                           size_t user_length, const void *range)
{
    bool success = false;
    pthread_mutex_lock(&lock);
    Entry *entry = find_entry(script);
    if (entry && slot == 0 && !input && output && !user_length && !range) {
        success = gpu_blur(context, entry, output);
    }
    pthread_mutex_unlock(&lock);
    if (!success) {
        stock_foreach(context, script, slot, input, output, user, user_length, range);
    }
}

static void destroy_script(const void *context, void *script)
{
    pthread_mutex_lock(&lock);
    Entry **cursor = &entries;
    while (*cursor) {
        if ((*cursor)->script == script) {
            Entry *entry = *cursor;
            *cursor = entry->next;
            free(entry);
            break;
        }
        cursor = &(*cursor)->next;
    }
    pthread_mutex_unlock(&lock);
    stock_destroy(context, script);
}

__attribute__((visibility("default")))
bool rsdHalInit(void *context, unsigned major, unsigned minor)
{
    pthread_once(&once, load_stock);
    if (!stock_init || !stock_init(context, major, minor)) {
        return false;
    }
    if (hooks_ready) {
        /* Verified stock ABI: driver pointer, then 86 HAL function pointers. */
        void **table = (void **)((uint8_t *)context + sizeof(void *));
        for (unsigned i = 0; i < HAL_POINTERS; i++) {
            if (table[i] == (void *)stock_intrinsic) table[i] = init_intrinsic;
            else if (table[i] == (void *)stock_var) table[i] = set_var;
            else if (table[i] == (void *)stock_object) table[i] = set_object;
            else if (table[i] == (void *)stock_foreach) table[i] = invoke_foreach;
            else if (table[i] == (void *)stock_destroy) table[i] = destroy_script;
        }
    }
    return true;
}
