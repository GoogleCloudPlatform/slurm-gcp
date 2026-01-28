# --- Configuration ---
NAME        := gcsfuse_spank
PREFIX      ?= /usr/local
LIBDIR      ?= $(PREFIX)/lib/slurm
SLURM_PATH  ?= /usr/include # Default path, change if slurm.h is elsewhere

# Compiler and Linker Settings
CC          ?= gcc
CFLAGS      := -ggdb -Wall -Wextra -std=gnu99 -fPIC
CPPFLAGS    := -I$(SLURM_PATH)
LDFLAGS     := -shared

# Source and Objects
SRCS        := gcsfuse_spank.c
OBJS        := $(SRCS:.c=.o)

# --- AddressSanitizer (ASan) Support ---
# Usage: make SANITIZE=1
ifdef SANITIZE
    CFLAGS  += -fsanitize=address -fsanitize=undefined -fno-omit-frame-pointer
    LDFLAGS += -fsanitize=address -fsanitize=undefined
endif

# --- Test Build Support ---
ifdef TEST_BUILD
    CFLAGS += -DTESTING_MODE
endif

# --- Targets ---

.PHONY: all install clean lint format analyze help test

all: $(NAME).so

test: test_main
	./test_main

test_main: tests/test_main.c gcsfuse_spank.c
	$(CC) $(CFLAGS) -DTESTING_MODE -I$(SLURM_PATH) -o test_main tests/test_main.c

# Link the shared object
$(NAME).so: $(OBJS)
	$(CC) $(LDFLAGS) -o $@ $^ $(LIBS)

# Compile source files
%.o: %.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

install: $(NAME).so
	install -d -m 755 $(LIBDIR)
	install -m 755 $(NAME).so $(LIBDIR)

# --- Analysis & Code Quality ---

# 1. Clang Static Analyzer (scan-build)
# Requires 'clang-tools' or 'clang' package
analyze: clean
	@echo "Running Clang Static Analyzer..."
	scan-build -o analysis_report --status-bugs \
		$(MAKE) CC=clang $(NAME).so

# 2. Cppcheck
lint:
	cppcheck --enable=all --suppress=missingIncludeSystem --error-exitcode=1 $(SRCS)

# 3. Formatting
format:
	clang-format -i $(SRCS)

clean:
	rm -f $(OBJS) $(NAME).so
	rm -rf analysis_report

help:
	@echo "Targets:"
	@echo "  all        : Build the plugin"
	@echo "  install    : Install to $(LIBDIR)"
	@echo "  analyze    : Run Clang static analyzer (scan-build)"
	@echo "  lint       : Run cppcheck"
	@echo "  format     : Run clang-format"
	@echo "  clean      : Remove build artifacts"
	@echo ""
	@echo "Options:"
	@echo "  SANITIZE=1 : Enable AddressSanitizer (ASan) for runtime leak checks"
