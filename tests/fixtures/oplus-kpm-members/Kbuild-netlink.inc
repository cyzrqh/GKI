ksu_netlink_cfg := $(shell sed -n '/struct netlink_kernel_cfg {/,/^};/p' $(srctree)/include/linux/netlink.h)
ifneq ($(findstring cb_mutex,$(ksu_netlink_cfg)),)
ccflags-y += -DKSU_NETLINK_CFG_HAS_CB_MUTEX=1
endif
ifneq ($(findstring compare,$(ksu_netlink_cfg)),)
ccflags-y += -DKSU_NETLINK_CFG_HAS_COMPARE=1
endif
