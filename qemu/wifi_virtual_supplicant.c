/* Android control interface for a virtual access point on virtio-net.
 * Traffic uses wlan0 and QEMU's user network; no physical radio is used.
 * SPDX-License-Identifier: Apache-2.0
 */
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <stddef.h>
#include <unistd.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

extern int property_set(const char *, const char *);

#define SSID "QEMU Wi-Fi"
#define BSSID "52:54:00:12:34:56"
#define MAC "52:54:00:25:00:01"

struct monitor_peer {
	int fd;
	struct sockaddr_un address;
	socklen_t length;
};
static struct monitor_peer monitors[16];
static int monitor_count, network_added, connected;
static volatile sig_atomic_t running = 1;
static long long scan_at, connect_at;
#define CONFIG_PATH "/data/misc/wifi/g925-virtual-network"

static void save_network(void)
{
	FILE *file = fopen(CONFIG_PATH ".tmp", "w");
	if (!file)
		return;
	fprintf(file, "%d\n", network_added);
	fclose(file);
	chmod(CONFIG_PATH ".tmp", 0660);
	chown(CONFIG_PATH ".tmp", 1010, 1010);
	rename(CONFIG_PATH ".tmp", CONFIG_PATH);
}

static unsigned long packet_count(const char *name)
{
	char path[128];
	unsigned long value = 0;
	snprintf(path, sizeof(path), "/sys/class/net/wlan0/statistics/%s", name);
	FILE *file = fopen(path, "r");
	if (file) {
		fscanf(file, "%lu", &value);
		fclose(file);
	}
	return value;
}

static long long now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000LL + ts.tv_nsec / 1000000;
}

static void send_event(const char *event)
{
	char buffer[1024];
	int length = snprintf(buffer, sizeof(buffer), "IFNAME=wlan0 <3>%s", event);
	for (int i = 0; i < monitor_count; i++)
		sendto(monitors[i].fd, buffer, length, 0,
			(struct sockaddr *)&monitors[i].address, monitors[i].length);
}

static int open_control(const char *path)
{
	struct sockaddr_un address = { .sun_family = AF_UNIX };
	int fd = socket(AF_UNIX, SOCK_DGRAM, 0);
	socklen_t length;
	if (fd < 0)
		return -1;
	if (path[0] == '@') {
		strcpy(address.sun_path + 1, path + 1);
		length = offsetof(struct sockaddr_un, sun_path) + strlen(path);
	} else {
		strcpy(address.sun_path, path);
		unlink(path);
		length = sizeof(address);
	}
	if (bind(fd, (struct sockaddr *)&address, length)) {
		perror(path);
		close(fd);
		return -1;
	}
	if (path[0] != '@')
		chmod(path, 0777);
	return fd;
}

static void command_reply(int fd)
{
	struct sockaddr_un peer = {0};
	socklen_t peer_length = sizeof(peer);
	char buffer[4096], reply[8192];
	int length = recvfrom(fd, buffer, sizeof(buffer) - 1, 0,
		(struct sockaddr *)&peer, &peer_length);
	if (length <= 0)
		return;
	buffer[length] = 0;
	char *command = buffer;
	if (!strncmp(command, "IFNAME=", 7)) {
		command = strchr(command, ' ');
		if (!command)
			return;
		command++;
	}
	strcpy(reply, "OK\n");
	if (!strcmp(command, "PING")) {
		strcpy(reply, "PONG\n");
	} else if (!strcmp(command, "ATTACH")) {
		if (monitor_count < 16) {
			monitors[monitor_count].fd = fd;
			monitors[monitor_count].address = peer;
			monitors[monitor_count++].length = peer_length;
		}
	} else if (!strcmp(command, "DETACH")) {
		for (int i = 0; i < monitor_count; i++) {
			if (monitors[i].fd == fd && monitors[i].length == peer_length &&
			    !memcmp(&monitors[i].address, &peer, peer_length)) {
				monitors[i] = monitors[--monitor_count];
				break;
			}
		}
	} else if (!strcmp(command, "SCAN") || !strncmp(command, "SCAN ", 5)) {
		scan_at = now_ms() + 250;
	} else if (!strcmp(command, "SCAN_RESULTS")) {
		snprintf(reply, sizeof(reply), "bssid / frequency / signal level / flags / ssid\n"
			BSSID "\t2437\t-40\t[ESS]\t" SSID "\n");
	} else if (!strncmp(command, "BSS ", 4)) {
		if (!strncmp(command, "BSS NEXT", 8) || strstr(command, "RANGE=1-")) {
			strcpy(reply, "####\n");
		} else {
			snprintf(reply, sizeof(reply), "id=0\nbssid=" BSSID "\nfreq=2437\n"
				"beacon_int=100\ncapabilities=0x0001\nlevel=-40\ntsf=%lld\n"
				"age=0\nflags=[ESS]\nssid=" SSID "\n"
				"ie=000a51454d552057692d4669\n====\n####\n", now_ms() * 1000);
		}
	} else if (!strncmp(command, "STATUS", 6)) {
		if (connected)
			snprintf(reply, sizeof(reply), "bssid=" BSSID "\nssid=" SSID "\nid=0\n"
				"mode=station\nwpa_state=COMPLETED\nkey_mgmt=NONE\n"
				"pairwise_cipher=NONE\ngroup_cipher=NONE\naddress=" MAC "\n");
		else
			strcpy(reply, "wpa_state=DISCONNECTED\naddress=" MAC "\n");
	} else if (!strcmp(command, "INTERFACES")) {
		strcpy(reply, "wlan0\np2p0\n");
	} else if (!strcmp(command, "ADD_NETWORK")) {
		network_added = 1;
		strcpy(reply, "0\n");
	} else if (!strcmp(command, "SAVE_CONFIG")) {
		save_network();
	} else if (!strcmp(command, "LIST_NETWORKS")) {
		strcpy(reply, "network id / ssid / bssid / flags\n");
		if (network_added)
			snprintf(reply + strlen(reply), sizeof(reply) - strlen(reply),
				"0\t" SSID "\tany\t%s\n", connected ? "[CURRENT]" : "");
	} else if (!strncmp(command, "GET_NETWORK", 11)) {
		char field[64] = {0};
		int id = -1;
		sscanf(command, "GET_NETWORK %d %63s", &id, field);
		if (id != 0 || !network_added)
			strcpy(reply, "FAIL\n");
		else if (!strcmp(field, "ssid"))
			strcpy(reply, "\"" SSID "\"\n");
		else if (!strcmp(field, "bssid"))
			strcpy(reply, "any\n");
		else if (!strcmp(field, "key_mgmt"))
			strcpy(reply, "NONE\n");
		else if (!strcmp(field, "auth_alg"))
			strcpy(reply, "OPEN\n");
		else if (!strcmp(field, "priority") || !strcmp(field, "scan_ssid") || !strcmp(field, "disabled"))
			strcpy(reply, "0\n");
		else
			strcpy(reply, "FAIL\n");
	} else if (!strncmp(command, "SELECT_NETWORK", 14) ||
		   !strncmp(command, "ENABLE_NETWORK", 14) ||
		   !strcmp(command, "RECONNECT") || !strcmp(command, "REASSOCIATE")) {
		if (network_added)
			connect_at = now_ms() + 350;
	} else if (!strcmp(command, "DISCONNECT") || !strncmp(command, "DISABLE_NETWORK", 15)) {
		connected = 0;
		connect_at = 0;
		send_event("CTRL-EVENT-DISCONNECTED bssid=" BSSID " reason=3 locally_generated=1");
	} else if (!strncmp(command, "REMOVE_NETWORK", 14)) {
		network_added = connected = 0;
		connect_at = 0;
		save_network();
	} else if (!strcmp(command, "SIGNAL_POLL")) {
		strcpy(reply, "RSSI=-40\nLINKSPEED=72\nNOISE=-90\nFREQUENCY=2437\n");
	} else if (!strcmp(command, "PKTCNT_POLL")) {
		snprintf(reply, sizeof(reply), "TXGOOD=%lu\nTXBAD=%lu\nRXGOOD=%lu\n",
			 packet_count("tx_packets"), packet_count("tx_errors"), packet_count("rx_packets"));
	} else if (!strncmp(command, "DRIVER MACADDR", 14)) {
		strcpy(reply, "Macaddr = " MAC "\n");
	} else if (!strncmp(command, "DRIVER GETBAND", 14)) {
		strcpy(reply, "Band 0\n");
	} else if (!strncmp(command, "DRIVER RSSI", 11)) {
		strcpy(reply, SSID " rssi -40\n");
	} else if (!strncmp(command, "GET_CAPABILITY", 14)) {
		strcpy(reply, "NONE\n");
	} else if (!strcmp(command, "GET_VERSION")) {
		strcpy(reply, "2.0-g925-virtual\n");
	}
	sendto(fd, reply, strlen(reply), 0, (struct sockaddr *)&peer, peer_length);
}

static void stop(int number)
{
	running = 0;
}

int main(void)
{
	const char *paths[] = {
		"/data/misc/wifi/sockets/wlan0", "/data/misc/wifi/sockets/p2p0",
		"/data/system/wpa_supplicant/wlan0", "@android:wpa_wlan0",
	};
	struct pollfd sockets[4];
	freopen("/data/misc/wifi/virtual-supplicant.log", "a", stdout);
	setvbuf(stdout, NULL, _IOLBF, 0);
	mkdir("/data/misc/wifi/sockets", 0777);
	mkdir("/data/system/wpa_supplicant", 0777);
	chmod("/data/system/wpa_supplicant", 0770);
	chown("/data/system/wpa_supplicant", 1010, 1010);
	FILE *config = fopen(CONFIG_PATH, "r");
	if (config) {
		fscanf(config, "%d", &network_added);
		fclose(config);
	}
	for (int i = 0; i < 4; i++) {
		sockets[i].fd = open_control(paths[i]);
		sockets[i].events = POLLIN;
		if (sockets[i].fd < 0)
			return 1;
	}
	property_set("wlan.driver.status", "ok");
	signal(SIGTERM, stop);
	signal(SIGINT, stop);
	while (running) {
		/* Sleep until a command or the next scan/connection deadline.
		 * An idle virtual radio should not wake an emulated CPU 10 times
		 * per second, and events should not wait for a polling tick. */
		long long deadline = scan_at;
		if (connect_at && (!deadline || connect_at < deadline))
			deadline = connect_at;
		long long delay = deadline - now_ms();
		int timeout = !deadline ? -1 : delay > 0 ? (int)delay : 0;
		poll(sockets, 4, timeout);
		for (int i = 0; i < 4; i++)
			if (sockets[i].revents & POLLIN)
				command_reply(sockets[i].fd);
		long long now = now_ms();
		if (scan_at && now >= scan_at) {
			scan_at = 0;
			send_event("CTRL-EVENT-SCAN-RESULTS");
		}
		if (connect_at && now >= connect_at) {
			connect_at = 0;
			connected = 1;
			send_event("CTRL-EVENT-STATE-CHANGE id=0 state=9 BSSID=" BSSID " SSID=" SSID);
			send_event("CTRL-EVENT-CONNECTED - Connection to " BSSID " completed [id=0 id_str=]");
		}
	}
	return 0;
}
