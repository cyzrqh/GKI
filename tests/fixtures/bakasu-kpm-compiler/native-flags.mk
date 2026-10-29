ifeq ($(CONFIG_KPM),y)
kernelsu-objs += kpm/kpm.o
ifdef KBUILD_EXTMOD
ccflags-y += -DCONFIG_KPM=1
endif
endif

ccflags-y += -DKSU_VERSION=$(KSU_VERSION)
ccflags-y += -DKSU_VERSION_FULL=\"$(KSU_VERSION_FULL)\"
