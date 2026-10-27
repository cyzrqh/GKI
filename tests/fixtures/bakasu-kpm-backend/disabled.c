#include "harness.h"
#include "kernel/kpm/kpm.h"
int main(void)
{
    struct ksu_kpm_cmd cmd = { 0 };
    _Static_assert(sizeof(cmd) == 32, "stable command ABI even when disabled");
    ksu_kpm_init();
    assert(!registered);
    assert(do_kpm(&cmd) == -EOPNOTSUPP);
    ksu_kpm_exit();
    assert(!registered);
    puts("real disabled backend: no fake capability; ioctl explicitly unsupported");
    return 0;
}
