flash_boot() {
  local varlist i kernel ramdisk fdt cmdline comp part0 part1 vndrname needskernelpatch nocompflag signfail pk8 cert avbtype;

  cd $SPLITIMG;
  if [ -f "$BIN/mkimage" ]; then
    varlist="name arch os type comp addr ep";
  elif [ -f "$BIN/mkbootimg" -a -f "$BIN/unpackelf" -a -f boot.img-base ]; then
    mv -f cmdline.txt boot.img-cmdline 2>/dev/null;
    varlist="cmdline base pagesize kernel_offset ramdisk_offset tags_offset";
  fi;
  for i in $varlist; do
    if [ -f boot.img-$i ]; then
      eval local $i=\"$(cat boot.img-$i)\";
    fi;
  done;

  cd $AKHOME;
  for i in zImage zImage-dtb Image Image-dtb Image.gz Image.gz-dtb Image.bz2 Image.bz2-dtb Image.lzo Image.lzo-dtb Image.lzma Image.lzma-dtb Image.xz Image.xz-dtb Image.lz4 Image.lz4-dtb Image.fit; do
    if [ -f $i ]; then
      kernel=$AKHOME/$i;
      break;
    fi;
  done;
  if [ "$kernel" ]; then
    if [ -f "$BIN/mkmtkhdr" -a -f "$SPLITIMG/boot.img-base" ]; then
      mkmtkhdr --kernel $kernel;
      kernel=$kernel-mtk;
    fi;
  elif [ "$(ls $SPLITIMG/kernel* 2>/dev/null)" ]; then
    kernel=$(ls $SPLITIMG/kernel* | grep -v 'kernel_dtb' | tail -n1);
  fi;
  if [ "$(ls ramdisk-new.cpio* 2>/dev/null)" ]; then
    ramdisk=$AKHOME/$(ls ramdisk-new.cpio* | tail -n1);
  elif [ -f "$BIN/mkmtkhdr" -a -f "$SPLITIMG/boot.img-base" ]; then
    ramdisk=$SPLITIMG/ramdisk.cpio.gz-mtk;
  else
    ramdisk=$(ls $SPLITIMG/ramdisk.cpio* 2>/dev/null | tail -n1);
  fi;
  for fdt in dt recovery_dtbo dtb; do
    for i in $AKHOME/$fdt $AKHOME/$fdt.img $SPLITIMG/$fdt; do
      if [ -f $i ]; then
        eval local $fdt=$i;
        break;
      fi;
    done;
  done;

  cd $SPLITIMG;
  if [ -f "$BIN/mkimage" ]; then
    [ "$comp" == "uncompressed" ] && comp=none;
    part0=$kernel;
    case $type in
      Multi) part1=":$ramdisk";;
      RAMDisk) part0=$ramdisk;;
    esac;
    mkimage -A $arch -O $os -T $type -C $comp -a $addr -e $ep -n "$name" -d $part0$part1 $AKHOME/boot-new.img;
  elif [ -f "$BIN/elftool" ]; then
    [ "$dt" ] && dt="$dt,rpm";
    [ -f cmdline.txt ] && cmdline="cmdline.txt@cmdline";
    elftool pack -o $AKHOME/boot-new.img header=elftool_out/header $kernel $ramdisk,ramdisk $dt $cmdline;
  elif [ -f "$BIN/mboot" ]; then
    cp -f $kernel kernel;
    cp -f $ramdisk ramdisk.cpio.gz;
    mboot -d $SPLITIMG -f $AKHOME/boot-new.img;
  elif [ -f "$BIN/rkcrc" ]; then
    rkcrc -k $ramdisk $AKHOME/boot-new.img;
  elif [ -f "$BIN/mkbootimg" -a -f "$BIN/unpackelf" -a -f boot.img-base ]; then
    [ "$dt" ] && dt="--dt $dt";
    mkbootimg --kernel $kernel --ramdisk $ramdisk --cmdline "$cmdline" --base $base --pagesize $pagesize --kernel_offset $kernel_offset --ramdisk_offset $ramdisk_offset --tags_offset "$tags_offset" $dt --output $AKHOME/boot-new.img;
  else
    [ "$kernel" ] && cp -f $kernel kernel;
    [ "$ramdisk" ] && cp -f $ramdisk ramdisk.cpio;
    if [ -d vendor_ramdisk ]; then
      for i in vendor_ramdisk/*.cpio; do
        vndrname=$(basename $i .cpio);
        cp -f $AKHOME/$vndrname-new.cpio vendor_ramdisk/$vndrname.cpio;
      done;
    fi;
    [ "$dt" -a -f extra ] && cp -f $dt extra;
    for i in dtb recovery_dtbo; do
      [ "$(eval echo \$$i)" -a -f $i ] && cp -f $(eval echo \$$i) $i;
    done;
    case $kernel in
      *Image*)
        if [ ! "$magisk_patched" -a ! "$NO_MAGISK_CHECK" ]; then
          magiskboot cpio ramdisk.cpio test;
          magisk_patched=$?;
        fi;
        if [ "$magisk_patched" == 1 ]; then
          ui_print " " "Magisk detected! Patching kernel so reflashing Magisk is not necessary...";
          comp=$(magiskboot decompress kernel 2>&1 | grep -vE 'raw|zimage' | sed -n 's;.*\[\(.*\)\];\1;p');
          (magiskboot split $kernel || magiskboot decompress $kernel kernel) >&2;
          if [ $? != 0 -a "$comp" ] && $comp --help 2>/dev/null; then
            echo "Attempting kernel unpack with busybox $comp..." >&2;
            $comp -dc $kernel > kernel;
          fi;
          # legacy SAR kernel string skip_initramfs -> want_initramfs
          magiskboot hexpatch kernel 736B69705F696E697472616D6673 77616E745F696E697472616D6673 && needskernelpatch=1;
          if [ "$(file_getprop $AKHOME/anykernel.sh do.modules)" == 1 ] && [ "$(file_getprop $AKHOME/anykernel.sh do.systemless)" == 1 ]; then
            strings kernel 2>/dev/null | grep -E -m1 'Linux version.*#' > $AKHOME/vertmp;
          fi;
          if [ "$needskernelpatch" ]; then
            if [ "$comp" ]; then
              magiskboot compress=$comp kernel kernel.$comp;
              if [ $? != 0 ] && $comp --help 2>/dev/null; then
                echo "Attempting kernel repack with busybox $comp..." >&2;
                $comp -9c kernel > kernel.$comp;
              fi;
              mv -f kernel.$comp kernel;
            fi;
          else
            echo "Restoring untouched new kernel since no patching required..." >&2;
            (magiskboot split -n $kernel || cp -f $kernel kernel) >&2;
          fi;
          [ ! -f .magisk ] && magiskboot cpio ramdisk.cpio "extract .backup/.magisk .magisk";
          export $(cat .magisk);
          for fdt in dtb extra kernel_dtb recovery_dtbo; do
            [ -f $fdt ] && magiskboot dtb $fdt patch; # remove dtb verity/avb
          done;
        elif [ -d /data/data/me.weishu.kernelsu ] && [ "$(file_getprop $AKHOME/anykernel.sh do.modules)" == 1 ] && [ "$(file_getprop $AKHOME/anykernel.sh do.systemless)" == 1 ]; then
          ui_print " " "KernelSU detected! Setting up for kernel helper module...";
          comp=$(magiskboot decompress kernel 2>&1 | grep -vE 'raw|zimage' | sed -n 's;.*\[\(.*\)\];\1;p');
          (magiskboot split $kernel || magiskboot decompress $kernel kernel) >&2;
          if [ $? != 0 -a "$comp" ] && $comp --help 2>/dev/null; then
            echo "Attempting kernel unpack with busybox $comp..." >&2;
            $comp -dc $kernel > kernel;
          fi;
          strings kernel > stringstmp 2>/dev/null;
          if grep -q -E '^/data/adb/ksud$' stringstmp; then
            touch $AKHOME/kernelsu_patched;
            grep -E -m1 'Linux version.*#' stringstmp > $AKHOME/vertmp;
            [ -d $RAMDISK/overlay.d ] && ui_print " " "Warning: overlay.d detected in ramdisk but not currently supported by KernelSU!";
          else
            ui_print " " "Warning: No KernelSU support detected in kernel!";
          fi;
          rm -f stringstmp;
          if [ "$comp" ]; then
            magiskboot compress=$comp kernel kernel.$comp;
            if [ $? != 0 ] && $comp --help 2>/dev/null; then
              echo "Attempting kernel repack with busybox $comp..." >&2;
              $comp -9c kernel > kernel.$comp;
            fi;
            mv -f kernel.$comp kernel;
          fi;
        else
          case $kernel in
            *-dtb) rm -f kernel_dtb;;
          esac;
        fi;
        unset magisk_patched KEEPVERITY KEEPFORCEENCRYPT RECOVERYMODE VENDORBOOT PREINITDEVICE SHA1 RANDOMSEED; # leave PATCHVBMETAFLAG set for repack
      ;;
    esac;
    case $RAMDISK_COMPRESSION in
      none|cpio) nocompflag="-n";;
    esac;
    case $PATCH_VBMETA_FLAG in
      auto|"") [ "$PATCHVBMETAFLAG" ] || export PATCHVBMETAFLAG=false;;
      1) export PATCHVBMETAFLAG=true;;
      *) export PATCHVBMETAFLAG=false;;
    esac;
    magiskboot repack $nocompflag $BOOTIMG $AKHOME/boot-new.img;
  fi;
  if [ $? != 0 ]; then
    abort "Repacking image failed. Aborting...";
  fi;
  [ "$PATCHVBMETAFLAG" ] && unset PATCHVBMETAFLAG;
  [ -f .magisk ] && touch $AKHOME/magisk_patched;

  cd $AKHOME;
  if [ -f "$BIN/futility" -a -d "$BIN/chromeos" ]; then
    echo "Signing with CHROMEOS..." >&2;
    futility vbutil_kernel --pack boot-new-signed.img --keyblock $BIN/chromeos/kernel.keyblock --signprivate $BIN/chromeos/kernel_data_key.vbprivk --version 1 --vmlinuz boot-new.img --bootloader $BIN/chromeos/empty --config $BIN/chromeos/empty --arch arm --flags 0x1;
    [ $? != 0 ] && signfail=1;
  fi;
  if [ -d "$BIN/avb" ]; then
    pk8=$(ls $BIN/avb/*.pk8);
    cert=$(ls $BIN/avb/*.x509.*);
    case $BLOCK in
      *recovery*|*RECOVERY*|*SOS*) avbtype=recovery;;
      *) avbtype=boot;;
    esac;
    if [ -f "$BIN/boot_signer-dexed.jar" ]; then
      if [ -f /system/bin/dalvikvm ] && [ "$(/system/bin/dalvikvm -Xnoimage-dex2oat -cp $BIN/boot_signer-dexed.jar com.android.verity.BootSignature -verify boot.img 2>&1 | grep VALID)" ]; then
        echo "Signing with AVBv1 /$avbtype..." >&2;
        /system/bin/dalvikvm -Xnoimage-dex2oat -cp $BIN/boot_signer-dexed.jar com.android.verity.BootSignature /$avbtype boot-new.img $pk8 $cert boot-new-signed.img;
      fi;
    else
      if magiskboot verify boot.img; then
        echo "Signing with AVBv1 /$avbtype..." >&2;
        magiskboot sign-avb1 boot-new.img /$avbtype $cert $pk8;
      fi;
    fi;
  fi;
  if [ $? != 0 -o "$signfail" ]; then
    abort "Signing image failed. Aborting...";
  fi;
  mv -f boot-new-signed.img boot-new.img 2>/dev/null;

  if [ ! -f boot-new.img ]; then
    abort "No repacked image found to flash. Aborting...";
  elif [ "$(wc -c < boot-new.img)" -gt "$(wc -c < boot.img)" ]; then
    abort "New image larger than target partition. Aborting...";
  fi;
  blockdev --setrw $BLOCK 2>/dev/null;
  if [ -f "$BIN/flash_erase" -a -f "$BIN/nandwrite" ]; then
    flash_erase $BLOCK 0 0;
    nandwrite -p $BLOCK boot-new.img;
  elif [ "$CUSTOMDD" ]; then
    dd if=/dev/zero of=$BLOCK $CUSTOMDD 2>/dev/null;
    dd if=boot-new.img of=$BLOCK $CUSTOMDD;
  else
    cat boot-new.img /dev/zero > $BLOCK 2>/dev/null || true;
  fi;
  if [ $? != 0 ]; then
    abort "Flashing image failed. Aborting...";
  fi;
}
