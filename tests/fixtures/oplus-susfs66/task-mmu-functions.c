// SPDX-License-Identifier: GPL-2.0-only

static int show_smap(struct seq_file *m, void *v)
{
	struct vm_area_struct *pad_vma = get_pad_vma(v);
	struct vm_area_struct *vma = get_data_vma(v);
	struct mem_size_stats mss;

	memset(&mss, 0, sizeof(mss));

	if (!vma_pages(vma))
		goto show_pad;

	smap_gather_stats(vma, &mss, 0);

	show_map_vma(m, vma);

	SEQ_PUT_DEC("Size:           ", vma->vm_end - vma->vm_start);
	SEQ_PUT_DEC(" kB\nKernelPageSize: ", vma_kernel_pagesize(vma));
	SEQ_PUT_DEC(" kB\nMMUPageSize:    ", vma_mmu_pagesize(vma));
	seq_puts(m, " kB\n");

	__show_smap(m, &mss, false);

	seq_printf(m, "THPeligible:    %8u\n",
		   !!thp_vma_allowable_orders(vma, vma->vm_flags, true, false,
					      true, THP_ORDERS_ALL));

	if (arch_pkeys_enabled())
		seq_printf(m, "ProtectionKey:  %8u\n", vma_pkey(vma));
	show_smap_vma_flags(m, vma);

show_pad:
	show_map_pad_vma(vma, pad_vma, m, show_smap, true);

	return 0;
}

static int show_smaps_rollup(struct seq_file *m, void *v)
{
	struct proc_maps_private *priv = m->private;
	struct mem_size_stats mss;
	struct mm_struct *mm = priv->mm;
	struct vm_area_struct *vma;
	unsigned long vma_start = 0, last_vma_end = 0;
	int ret = 0;
	VMA_ITERATOR(vmi, mm, 0);

	priv->task = get_proc_task(priv->inode);
	if (!priv->task)
		return -ESRCH;

	if (!mm || !mmget_not_zero(mm)) {
		ret = -ESRCH;
		goto out_put_task;
	}

	memset(&mss, 0, sizeof(mss));

	ret = mmap_read_lock_killable(mm);
	if (ret)
		goto out_put_mm;

	hold_task_mempolicy(priv);
	vma = vma_next(&vmi);

	if (unlikely(!vma))
		goto empty_set;

	vma_start = vma->vm_start;
	do {
		smap_gather_stats(vma, &mss, 0);
		last_vma_end = vma->vm_end;

		/*
		 * Release mmap_lock temporarily if someone wants to
		 * access it for write request.
		 */
		if (mmap_lock_is_contended(mm)) {
			vma_iter_invalidate(&vmi);
			mmap_read_unlock(mm);
			ret = mmap_read_lock_killable(mm);
			if (ret) {
				release_task_mempolicy(priv);
				goto out_put_mm;
			}

			/*
			 * After dropping the lock, there are four cases to
			 * consider. See the following example for explanation.
			 *
			 *   +------+------+-----------+
			 *   | VMA1 | VMA2 | VMA3      |
			 *   +------+------+-----------+
			 *   |      |      |           |
			 *  4k     8k     16k         400k
			 *
			 * Suppose we drop the lock after reading VMA2 due to
			 * contention, then we get:
			 *
			 *	last_vma_end = 16k
			 *
			 * 1) VMA2 is freed, but VMA3 exists:
			 *
			 *    vma_next(vmi) will return VMA3.
			 *    In this case, just continue from VMA3.
			 *
			 * 2) VMA2 still exists:
			 *
			 *    vma_next(vmi) will return VMA3.
			 *    In this case, just continue from VMA3.
			 *
			 * 3) No more VMAs can be found:
			 *
			 *    vma_next(vmi) will return NULL.
			 *    No more things to do, just break.
			 *
			 * 4) (last_vma_end - 1) is the middle of a vma (VMA'):
			 *
			 *    vma_next(vmi) will return VMA' whose range
			 *    contains last_vma_end.
			 *    Iterate VMA' from last_vma_end.
			 */
			vma = vma_next(&vmi);
			/* Case 3 above */
			if (!vma)
				break;

			/* Case 1 and 2 above */
			if (vma->vm_start >= last_vma_end)
				continue;

			/* Case 4 above */
			if (vma->vm_end > last_vma_end)
				smap_gather_stats(vma, &mss, last_vma_end);
		}
	} for_each_vma(vmi, vma);

empty_set:
	show_vma_header_prefix(m, vma_start, last_vma_end, 0, 0, 0, 0);
	seq_pad(m, ' ');
	seq_puts(m, "[rollup]\n");

	__show_smap(m, &mss, true);

	release_task_mempolicy(priv);
	mmap_read_unlock(mm);

out_put_mm:
	mmput(mm);
out_put_task:
	put_task_struct(priv->task);
	priv->task = NULL;

	return ret;
}
