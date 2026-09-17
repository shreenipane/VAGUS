/* irm-test launcher: the coding agent's only command (Intelligent Resource Manager, SECURITY.md T2).
 *
 * agy's command rules accept environment prefixes such as LD_PRELOAD=... or PATH=... (BUILD_LOG D.6). A dynamically
 * linked program or a shell script would load attacker-chosen code before any check could run. This binary is static
 * with no libc, so there is no dynamic loader to honour those variables and nothing reads the environment. It execs the
 * jail script with a fixed argv prefix and a fixed environment.
 *
 * Build: gcc -static -nostdlib -ffreestanding -fno-builtin -fno-stack-protector -fno-pie -no-pie -O2 \
 *            -o ~/.local/bin/irm-test ~/.local/lib/irm/irm-test.c
 * x86_64 Linux only (raw syscall numbers). */

#define SCRIPT "/home/shreenipane/.local/lib/irm/irm-test.sh"
#define MAXARGS 128
#define SYS_execve 59
#define SYS_exit 60

long sys3(long n, long a, long b, long c);

__asm__(
    ".text\n"
    ".global sys3\n"
    "sys3:\n"
    "  mov %rdi, %rax\n"
    "  mov %rsi, %rdi\n"
    "  mov %rdx, %rsi\n"
    "  mov %rcx, %rdx\n"
    "  syscall\n"
    "  ret\n"
    ".global _start\n"
    "_start:\n"
    "  mov %rsp, %rdi\n"      /* rdi = pointer to argc on the initial stack */
    "  and $-16, %rsp\n"      /* SysV ABI: stack aligned before the call */
    "  call start\n"
    "  hlt\n");

void start(long *sp)
{
    long argc = sp[0];
    char **argv = (char **)(sp + 1);
    char *args[MAXARGS + 4];
    char *env[2];
    long i;

    if (argc > MAXARGS)
        sys3(SYS_exit, 2, 0, 0);
    /* Assigned one by one: an initializer list could make the compiler emit a memset call, and there is no libc. */
    args[0] = "/bin/bash";
    args[1] = "-p";
    args[2] = SCRIPT;
    for (i = 1; i < argc; i++)
        args[i + 2] = argv[i];
    args[argc + 2] = 0;
    env[0] = "PATH=/usr/bin";
    env[1] = 0;
    sys3(SYS_execve, (long)args[0], (long)args, (long)env);
    sys3(SYS_exit, 127, 0, 0);
}
