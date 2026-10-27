/* Real ported C is compiled below. The Linux-only host fixtures simulate the
 * authenticated ioctl caller and official KP ABI2 trampoline, not a device.
 */
#include "harness.h"
#include "kernel/kpm/kpm.c"
_Static_assert(sizeof(struct ksu_kpm_cmd) == 32, "KPM command ABI size");
_Static_assert(offsetof(struct ksu_kpm_cmd, control_code) == 0, "opcode offset");
_Static_assert(offsetof(struct ksu_kpm_cmd, arg1) == 8, "arg1 offset");
_Static_assert(offsetof(struct ksu_kpm_cmd, arg2) == 16, "arg2 offset");
_Static_assert(offsetof(struct ksu_kpm_cmd, result_code) == 24, "result ptr offset");
_Static_assert(KSU_FEATURE_KPM == 7, "KPM cannot replace adb_root");
_Static_assert(KSU_FEATURE_ADB_ROOT == 3, "preserve existing feature ID");
_Static_assert(sizeof(int) == 4 && sizeof(long) == 8, "LP64 harness");
static bool lookup_missing = true;
static int abi = 2, operation_calls;
static const char *user_path, *user_args;
int ksu_register_feature_handler(const struct ksu_feature_handler *h)
{
    assert(!registered);
    registered = h;
    return 0;
}
int ksu_unregister_feature_handler(u32 id)
{
    assert(id == 7);
    registered = NULL;
    return 0;
}
static long kernel_truncate(const struct pt_regs *r)
{
    assert(r->pstate == PSR_MODE_EL1h);
    assert(r->regs[0] == SUKISU_KPM_SUPERCALL_MAGIC);
    assert(r->regs[6] == SUKISU_KPM_SUPERCALL_MAGIC2);
    assert(r->regs[7] == SUKISU_KPM_SUPERCALL_MAGIC3);
    assert(r->regs[8] == 2);
    for (int i = 9; i < 31; i++) assert(!r->regs[i]);
    unsigned long op = r->regs[1], a1 = r->regs[2], a2 = r->regs[3], a3 = r->regs[4];
    assert(!r->regs[5]);
    if (!op) {
        assert(!a1 && !a2 && !a3);
        return abi;
    }
    operation_calls++;
    switch (op) {
    case 1:
        assert(a1 && a1 != (uintptr_t)user_path);
        assert(!strcmp((char *)a1, "/data/adb/test.kpm"));
        assert(a2 && a2 != (uintptr_t)user_args);
        assert(!strcmp((char *)a2, "flag=1"));
        assert(!a3);
        return 0;
    case 2:
        assert(!strcmp((char *)a1, "test"));
        assert(!a2 && !a3);
        return 0;
    case 3:
        assert(!a1 && !a2 && !a3);
        return 2;
    case 4:
        assert(a1 && a2 == 64 && !a3);
        strcpy((char *)a1, "alpha\nbeta\n");
        return strlen((char *)a1);
    case 5:
        assert(!strcmp((char *)a1, "test"));
        assert(a2 && a3 == 256);
        strcpy((char *)a2, "name=test\nversion=1");
        return strlen((char *)a2);
    case 6:
        assert(!strcmp((char *)a1, "test"));
        assert(!strcmp((char *)a2, "status"));
        assert(!a3);
        return 17;
    case 7:
        assert(a1 && a2 && a2 <= 4096 && !a3);
        return snprintf((char *)a1, a2, "0.14.0 ABI2 fixture");
    case 8:
        assert(!strcmp((char *)a1, "/data/adb/test.kpm"));
        assert(!strcmp((char *)a2, "flag=1"));
        assert(!strcmp((char *)a3, "post-fs-data"));
        return 0;
    default:
        return -EOPNOTSUPP;
    }
}
unsigned long find_kernel_symbol_exact(const char *name)
{
    assert(!strncmp(name, "__arm64_sys_truncate", 20));
    return lookup_missing ? 0 : (unsigned long)(uintptr_t)kernel_truncate;
}
static int invoke(u64 op, u64 a1, u64 a2)
{
    int result = 0x12345678;
    struct ksu_kpm_cmd cmd = { op, a1, a2, (uintptr_t)&result };
    assert(do_kpm(&cmd) == 0);
    assert(result != 0x12345678);
    assert(allocations == 0);
    return result;
}
int main(void)
{
    char buf[4096] = { 0 };
    assert(invoke(7, (uintptr_t)buf, 64) == -EOPNOTSUPP);
    lookup_missing = false;
    abi = 1;
    assert(invoke(7, (uintptr_t)buf, 64) == -EOPNOTSUPP);
    assert(!operation_calls);
    abi = 2;
    assert(invoke(7, (uintptr_t)buf, 64) == 0);
    assert(!strcmp(buf, "0.14.0 ABI2 fixture"));
    assert(invoke(3, 0, 0) == 2);
    user_path = "/data/adb/test.kpm";
    user_args = "flag=1";
    assert(invoke(1, (uintptr_t)user_path, (uintptr_t)user_args) == 0);
    assert(invoke(2, (uintptr_t)"test", 0) == 0);
    assert(invoke(4, (uintptr_t)buf, 64) == 11);
    assert(!strcmp(buf, "alpha\nbeta\n"));
    assert(invoke(5, (uintptr_t)"test", (uintptr_t)buf) == 0);
    assert(!strcmp(buf, "name=test\nversion=1"));
    assert(invoke(6, (uintptr_t)"test", (uintptr_t)"status") == 17);
    struct ksu_kpm_load_event_cmd event = { (uintptr_t)user_args, (uintptr_t)"post-fs-data" };
    assert(invoke(8, (uintptr_t)user_path, (uintptr_t)&event) == 0);
    assert(invoke(99, 0, 0) == -EOPNOTSUPP);
    assert(invoke(0x1000000000000001UL, 0, 0) == -EOPNOTSUPP);
    assert(invoke(1, 0, (uintptr_t)user_args) == -EINVAL);
    assert(invoke(1, (uintptr_t)"", (uintptr_t)user_args) == -EINVAL);
    assert(invoke(7, (uintptr_t)buf, 0) == -EINVAL);
    assert(invoke(7, (uintptr_t)buf, 4097) == -EINVAL);
    assert(invoke(7, (uintptr_t)buf, 4) == -ENOBUFS);
    assert(invoke(7, 0, 64) == -EFAULT);
    event.event = 0;
    assert(invoke(8, (uintptr_t)user_path, (uintptr_t)&event) == -EINVAL);
    assert(do_kpm(NULL) == -EFAULT);
    struct ksu_kpm_cmd bad = { 7, (uintptr_t)buf, 64, 1 };
    assert(do_kpm(&bad) == -EFAULT);
    char long_path[1025];
    memset(long_path, 'x', sizeof(long_path));
    long_path[1024] = 0;
    assert(invoke(1, (uintptr_t)long_path, (uintptr_t)user_args) == -ENAMETOOLONG);
    fail_allocation = 1;
    assert(invoke(7, (uintptr_t)buf, 64) == -ENOMEM);
    fail_allocation = 0;
    ksu_kpm_init();
    assert(registered && registered->feature_id == 7 && !registered->set_handler);
    u64 available = 99;
    assert(!registered->get_handler(&available) && available == 1);
    abi = 1;
    assert(!registered->get_handler(&available) && available == 0);
    ksu_kpm_exit();
    assert(!registered);
    puts("real backend C: standard operations, ABI2 framing, negative probes, pointer results, bounds, cleanup and read-only capability passed");
    return 0;
}
