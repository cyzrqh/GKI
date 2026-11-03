ifneq ($(KBUILD_EXTMOD),)
# 6.18 exports the external module source as srcroot when M/MO are used.
ifneq ($(srcroot),)
KSU_KERNEL_DIR := $(srcroot)
else
KSU_KERNEL_DIR := $(KBUILD_EXTMOD)
endif
else ifeq ($(filter /%,$(src)),)
KSU_KERNEL_DIR := $(srctree)/$(src)
else
KSU_KERNEL_DIR := $(src)
endif

ccflags-y += -I$(KSU_KERNEL_DIR) -I$(KSU_KERNEL_DIR)/include
