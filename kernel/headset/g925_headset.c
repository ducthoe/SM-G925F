// SPDX-License-Identifier: GPL-2.0-only
/* Headphone jack state used by Samsung's WiredAccessoryManager. */
#include <linux/module.h>
#include <linux/device.h>
#include <linux/err.h>
#include <linux/init.h>
#include <linux/kdev_t.h>

static struct class *switch_class;
static struct device *headphones;
static int state = 2; /* Android h2w: headphones without a microphone. */

static ssize_t state_show(struct device *dev, struct device_attribute *attr, char *buffer)
{
	return sprintf(buffer, "%d\n", state);
}

static ssize_t state_store(struct device *dev, struct device_attribute *attr,
			   const char *buffer, size_t length)
{
	int value;
	char event[32];
	char *environment[] = { "SWITCH_NAME=h2w", event, NULL };
	if (kstrtoint(buffer, 10, &value) || value < 0 || value > 2)
		return -EINVAL;
	state = value;
	snprintf(event, sizeof(event), "SWITCH_STATE=%d", state);
	kobject_uevent_env(&headphones->kobj, KOBJ_CHANGE, environment);
	return length;
}

static ssize_t name_show(struct device *dev, struct device_attribute *attr, char *buffer)
{
	return sprintf(buffer, "QEMU headphones\n");
}
static DEVICE_ATTR(state, 0644, state_show, state_store);
static DEVICE_ATTR(name, 0444, name_show, NULL);

static int __init headset_init(void)
{
	int error;
	switch_class = class_create(THIS_MODULE, "switch");
	if (IS_ERR(switch_class))
		return PTR_ERR(switch_class);
	headphones = device_create(switch_class, NULL, MKDEV(0, 0), NULL, "h2w");
	if (IS_ERR(headphones)) {
		class_destroy(switch_class);
		return PTR_ERR(headphones);
	}
	error = device_create_file(headphones, &dev_attr_state);
	if (!error)
		error = device_create_file(headphones, &dev_attr_name);
	if (error) {
		device_unregister(headphones);
		class_destroy(switch_class);
		return error;
	}
	pr_info("g925: virtual headphone jack connected\n");
	return 0;
}

static void __exit headset_exit(void)
{
	device_remove_file(headphones, &dev_attr_state);
	device_remove_file(headphones, &dev_attr_name);
	device_unregister(headphones);
	class_destroy(switch_class);
}
module_init(headset_init);
module_exit(headset_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("SM-G925F virtual headphone jack");
