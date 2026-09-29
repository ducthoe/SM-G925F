#include <linux/module.h>
#include <linux/init.h>

/* Samsung netd changes these firmware selectors before starting Wi-Fi. */
static char *firmware_path = "/system/etc/wifi/bcmdhd_sta.bin";
static char *nvram_path = "/system/etc/wifi/nvram_net.txt";
module_param(firmware_path, charp, 0644);
module_param(nvram_path, charp, 0644);

static int __init virtual_wifi_init(void)
{
	pr_info("dhd: virtual firmware selectors for the virtio Wi-Fi adapter\n");
	return 0;
}

static void __exit virtual_wifi_exit(void) { }
module_init(virtual_wifi_init);
module_exit(virtual_wifi_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Samsung firmware selector compatibility for virtual Wi-Fi");
