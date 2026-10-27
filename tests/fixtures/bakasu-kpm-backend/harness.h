#ifndef BACKEND_HARNESS_H
#define BACKEND_HARNESS_H
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
/* glibc bits/errno.h imports linux/errno.h, which this harness stubs.
 * Read the real Linux errno values without recursively entering that stub. */
#include <asm-generic/errno.h>
typedef uint64_t u64;
typedef uint32_t u32;
typedef uint64_t __u64;
typedef uint32_t __u32;
#define __aligned_u64 uint64_t __attribute__((aligned(8)))
#define __user
#define __init
#define __exit
#define __nocfi
#define GFP_KERNEL 0
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x, y) ((x) = (y))
#define PSR_MODE_EL1h 5UL
#define pr_err(...) fprintf(stderr, __VA_ARGS__)
struct pt_regs { unsigned long regs[31]; unsigned long pstate; };
static int allocations, fail_allocation;
static void *test_alloc(size_t n, bool zero)
{
    if (fail_allocation) return NULL;
    void *p = malloc(n);
    if (p) {
        memset(p, zero ? 0 : 0xa5, n);
        allocations++;
    }
    return p;
}
static void test_free(void *p) { if (p) { allocations--; free(p); } }
#define kmalloc(n, f) test_alloc(n, false)
#define kzalloc(n, f) test_alloc(n, true)
#define kfree(p) test_free(p)
static bool access_ok(const void *p, size_t n)
{
    return (uintptr_t)p >= 4096 && n <= 4096;
}
static int test_copy(void *dst, const void *src, size_t n)
{
    if (!access_ok(dst, n) || !access_ok(src, n)) return 1;
    memcpy(dst, src, n);
    return 0;
}
#define copy_from_user(d, s, n) test_copy((d), (s), (n))
#define copy_to_user(d, s, n) test_copy((d), (s), (n))
#define u64_to_user_ptr(x) ((void *)(uintptr_t)(x))
static long strncpy_from_user(char *d, const char *s, long cap)
{
    if (!access_ok(s, cap)) return -EFAULT;
    size_t n = strnlen(s, (size_t)cap);
    memcpy(d, s, n);
    if (n < (size_t)cap) d[n] = 0;
    return (long)n;
}
/* Host model of the existing feature registry interface. Static tests read
 * the production diff separately to verify that adb_root remains 3 and the
 * new capability really is 7; no backend implementation is mirrored here. */
enum { KSU_FEATURE_ADB_ROOT = 3, KSU_FEATURE_KPM = 7, KSU_FEATURE_MAX = 8 };
typedef int (*get_handler_t)(u64 *);
typedef int (*set_handler_t)(u64);
struct ksu_feature_handler {
    u32 feature_id;
    const char *name;
    get_handler_t get_handler;
    set_handler_t set_handler;
};
static const struct ksu_feature_handler *registered;
int ksu_register_feature_handler(const struct ksu_feature_handler *h);
int ksu_unregister_feature_handler(u32 id);
unsigned long find_kernel_symbol_exact(const char *name);
#endif
