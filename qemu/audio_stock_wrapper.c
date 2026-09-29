/* Keep Samsung's original audio callback layout and disable capture reads.
 * SPDX-License-Identifier: Apache-2.0
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <elf.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <hardware/hardware.h>

#ifdef __LP64__
typedef Elf64_Ehdr Header;
typedef Elf64_Phdr Program;
typedef Elf64_Dyn Dynamic;
typedef Elf64_Sym Symbol;
typedef Elf64_Rela Relocation;
#define SYMBOL_INDEX ELF64_R_SYM
#else
typedef Elf32_Ehdr Header;
typedef Elf32_Phdr Program;
typedef Elf32_Dyn Dynamic;
typedef Elf32_Sym Symbol;
typedef Elf32_Rel Relocation;
#define SYMBOL_INDEX ELF32_R_SYM
#endif

static hw_module_t *original;
hw_module_t HAL_MODULE_INFO_SYM = { .tag = HARDWARE_MODULE_TAG, .module_api_version = 1, .hal_api_version = HARDWARE_HAL_API_VERSION, .id = "audio", .name = "G925 audio", .author = "Emulator" };

static ssize_t no_capture(int fd, void *buffer, size_t length)
{
	/* The virtual jack has no microphone. An empty capture read does not
	 * wait on the playback FIFO while holding the stock driver's lock. */
	return 0;
}

static uintptr_t address(uintptr_t base, uintptr_t value)
{
	return value < base ? base + value : value;
}

static int patch_capture(void *module)
{
	Dl_info info;
	if (!dladdr(module, &info))
		return -1;
	uintptr_t base = (uintptr_t)info.dli_fbase;
	Header *header = (Header *)base;
	Program *program = (Program *)(base + header->e_phoff);
	Dynamic *dynamic = NULL;
	for (int i = 0; i < header->e_phnum; i++)
		if (program[i].p_type == PT_DYNAMIC)
			dynamic = (Dynamic *)(base + program[i].p_vaddr);
	if (!dynamic)
		return -1;
	Symbol *symbols = NULL;
	const char *strings = NULL;
	Relocation *relocations = NULL;
	size_t size = 0;
	for (; dynamic->d_tag != DT_NULL; dynamic++) {
		switch (dynamic->d_tag) {
		case DT_SYMTAB: symbols = (Symbol *)address(base, dynamic->d_un.d_ptr); break;
		case DT_STRTAB: strings = (const char *)address(base, dynamic->d_un.d_ptr); break;
		case DT_JMPREL: relocations = (Relocation *)address(base, dynamic->d_un.d_ptr); break;
		case DT_PLTRELSZ: size = dynamic->d_un.d_val; break;
		}
	}
	if (!symbols || !strings || !relocations || size > 65536)
		return -1;
	for (size_t i = 0; i < size / sizeof(*relocations); i++) {
		Symbol *symbol = &symbols[SYMBOL_INDEX(relocations[i].r_info)];
		if (!strcmp(strings + symbol->st_name, "read")) {
			uintptr_t *slot = (uintptr_t *)(base + relocations[i].r_offset);
			uintptr_t page = (uintptr_t)slot & ~(uintptr_t)(getpagesize() - 1);
			if (mprotect((void *)page, getpagesize(), PROT_READ | PROT_WRITE))
				return -1;
			*slot = (uintptr_t)no_capture;
			return 0;
		}
	}
	return -1;
}

static int open_original(const hw_module_t *module, const char *name, hw_device_t **device)
{
	if (!original)
		return -ENODEV;
	return original->methods->open(original, name, device);
}
static hw_module_methods_t methods = { .open = open_original };

__attribute__((constructor)) static void initialize(void)
{
#ifdef __LP64__
	const char *path = "/system/lib64/hw/audio.primary.goldfish.so";
#else
	const char *path = "/system/lib/hw/audio.primary.goldfish.so";
#endif
	void *library = dlopen(path, RTLD_NOW | RTLD_LOCAL);
	if (!library) {
		fprintf(stderr, "Stock audio load failed: %s\n", dlerror());
		return;
	}
	original = dlsym(library, "HMI");
	if (!original || patch_capture(original)) {
		fprintf(stderr, "Stock audio capture hook was not found\n");
		original = NULL;
		return;
	}
	HAL_MODULE_INFO_SYM = *original;
	HAL_MODULE_INFO_SYM.methods = &methods;
}
