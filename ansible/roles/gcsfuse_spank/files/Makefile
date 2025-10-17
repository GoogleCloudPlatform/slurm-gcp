# This prevents conflicts with files of the same name and can improve performance.
.PHONY: all install clean uninstall

# --- Variables ---
NAME    := gcsfuse_spank
SRCS    := gcsfuse_spank.c
OBJS    := $(SRCS:.c=.o)

# Using standard variable names (e.g., CFLAGS, LDFLAGS) is conventional.
# Using += allows these to be appended to from the command line.
CFLAGS  += -ggdb -Wall -fPIC
LDFLAGS += -shared
LDLIBS  += -lslurm

INSTALL_DIR := /usr/local/lib/slurm

# --- Targets ---

all: $(NAME).so

# The install target should depend on the binary being built.
install: $(NAME).so
	@echo "Installing $(NAME).so to $(INSTALL_DIR)..."
	@mkdir -p --mode=0755 $(INSTALL_DIR)
	@install -m0755 $(NAME).so $(INSTALL_DIR)/

# The linking rule to create the final shared object.
$(NAME).so: $(OBJS)
	$(CC) $(LDFLAGS) -o $@ $^ $(LDLIBS)

# A pattern rule is a more modern and flexible way to compile .c files to .o files
# than the old-style .c.o suffix rule.
%.o: %.c
	$(CC) $(CFLAGS) -c $< -o $@

# Removes all generated files.
clean:
	@echo "Cleaning up generated files..."
	@-rm -f *.o *.so

# Provides a way to remove the installed file.
uninstall:
	@echo "Uninstalling from $(INSTALL_DIR)..."
	@-rm -f $(INSTALL_DIR)/$(NAME).so
