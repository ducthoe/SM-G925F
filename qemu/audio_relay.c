/* Pace the stock goldfish HAL's PCM FIFO and send bounded UDP packets.
 * SPDX-License-Identifier: Apache-2.0
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define PCM_BYTES_PER_SECOND 176400
#define PACKET_BYTES 1024

static int64_t monotonic_ns(void)
{
	struct timespec now;
	clock_gettime(CLOCK_MONOTONIC, &now);
	return (int64_t)now.tv_sec * 1000000000 + now.tv_nsec;
}

int main(int argc, char **argv)
{
	if (argc != 2)
		return 2;
	int port = atoi(argv[1]);
	if (port < 1 || port > 65535)
		return 2;
	signal(SIGPIPE, SIG_IGN);
	struct sockaddr_in host = { .sin_family = AF_INET, .sin_port = htons(port) };
	inet_pton(AF_INET, "10.0.2.2", &host.sin_addr);
	/* RDWR keeps EOF from spinning between previews. Playback is the only
	 * writer; the HAL wrapper disables the nonexistent microphone. */
	int fifo = open("/dev/eac", O_RDWR | O_NONBLOCK);
	int connection = socket(AF_INET, SOCK_DGRAM | SOCK_NONBLOCK, 0);
	if (fifo < 0 || connection < 0)
		return 1;
	fcntl(fifo, F_SETPIPE_SZ, 8192); /* 46 ms, instead of seconds in TCP. */
	int size = 4096;
	setsockopt(connection, SOL_SOCKET, SO_SNDBUF, &size, sizeof(size));
	struct pollfd input = { .fd = fifo, .events = POLLIN };
	struct {
		uint32_t magic;
		uint32_t sequence;
		unsigned char pcm[PACKET_BYTES];
	} packet;
	packet.magic = htonl(0x47393235); /* G925 */
	uint32_t sequence = 0;
	int64_t deadline = 0;
	fprintf(stderr, "Paced UDP audio: 10.0.2.2:%d, FIFO 46 ms\n", port);
	for (;;) {
		if (poll(&input, 1, -1) < 0)
			continue;
		ssize_t length = read(fifo, packet.pcm, sizeof(packet.pcm));
		if (length <= 0)
			continue;
		int64_t now = monotonic_ns();
		/* Never rush through a backlog after the VM was descheduled. */
		if (deadline < now - 30000000)
			deadline = now;
		while (deadline > now) {
			int64_t delay = deadline - now;
			struct timespec pause = { .tv_sec = delay / 1000000000,
				.tv_nsec = delay % 1000000000 };
			nanosleep(&pause, NULL);
			now = monotonic_ns();
		}
		packet.sequence = htonl(sequence++);
		/* Failed or congested packets are discarded, never replayed later. */
		sendto(connection, &packet, length + 8, MSG_DONTWAIT,
			(struct sockaddr *)&host, sizeof(host));
		deadline += length * 1000000000LL / PCM_BYTES_PER_SECOND;
	}
}
