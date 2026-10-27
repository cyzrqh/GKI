DYNAMIC_STRUCT_BEGIN(netlink_kernel_cfg)
DEFINE_MEMBER(netlink_kernel_cfg, groups)
DEFINE_MEMBER(netlink_kernel_cfg, flags)
DEFINE_MEMBER(netlink_kernel_cfg, input)
#ifdef KSU_NETLINK_CFG_HAS_CB_MUTEX
DEFINE_MEMBER(netlink_kernel_cfg, cb_mutex)
#endif
DEFINE_MEMBER(netlink_kernel_cfg, bind)
DEFINE_MEMBER(netlink_kernel_cfg, unbind)
#ifdef KSU_NETLINK_CFG_HAS_COMPARE
DEFINE_MEMBER(netlink_kernel_cfg, compare)
#endif
DYNAMIC_STRUCT_END(netlink_kernel_cfg)
