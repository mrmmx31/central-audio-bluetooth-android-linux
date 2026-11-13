# SPDX-License-Identifier: GPL-2.0-only
CC ?= cc
PKG_CONFIG ?= pkg-config
CFLAGS += -Wall -Wextra -Wformat=2 -Wstrict-prototypes -Wmissing-prototypes
GTK_CFLAGS := $(shell $(PKG_CONFIG) --cflags gtk+-2.0)
GTK_LIBS := $(shell $(PKG_CONFIG) --libs gtk+-2.0)

.PHONY: all clean

all: audio-center

audio-center: audio-center.c audio-route.sh
	$(CC) $(CPPFLAGS) $(CFLAGS) $(GTK_CFLAGS) -o $@ audio-center.c $(LDFLAGS) $(GTK_LIBS)

clean:
	rm -f audio-center
