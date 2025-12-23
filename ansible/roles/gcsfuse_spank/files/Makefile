NAME    := gcsfuse_spank
DESTDIR ?= /usr/local
LIBDIR  ?= $(DESTDIR)/lib/slurm

FLAGS   := -ggdb -Wall
SHOPTS  := -shared
LLIBS   := -lslurm
OBJS    := gcsfuse_spank.o

# Helper to detect if running tests
ifdef TEST_BUILD
CFLAGS += -DTESTING_MODE
endif

all: $(NAME).so

install: $(NAME).so
	mkdir -p --mode=0755 $(LIBDIR)
	install -m0755 $(NAME).so $(LIBDIR)

$(NAME).so: $(OBJS)
	$(CC) $(SHOPTS) -o $(NAME).so $(OBJS) $(LLIBS)

gcsfuse_spank.o: gcsfuse_spank.c
	$(CC) $(FLAGS) -fPIC -c gcsfuse_spank.c

# --- Test Targets ---

test: test_main
	./test_main

test_main: tests/test_main.c gcsfuse_spank.c
	$(CC) -ggdb -Wall -DTESTING_MODE -I../slurm -o test_main tests/test_main.c

# --- Lint/Format Targets ---

lint:
	cppcheck --enable=all --suppress=missingIncludeSystem gcsfuse_spank.c

format:
	clang-format -i gcsfuse_spank.c tests/test_main.c

clean:
	rm -f *.o *.so test_main
