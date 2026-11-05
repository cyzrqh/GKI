#ifdef CONFIG_KSU_SUSFS
void security_dump_masked_av_fn(struct policydb *policydb,
				    struct context *scontext,
				    struct context *tcontext,
				    u16 tclass,
				    u32 permissions,
				    const char *reason)
{
	security_dump_masked_av(policydb, scontext, tcontext, tclass, permissions, reason);
}

void context_struct_compute_av_fn(struct policydb *policydb,
				      struct context *scontext,
				      struct context *tcontext,
				      u16 tclass,
				      struct av_decision *avd,
				      struct extended_perms *xperms)
{
	context_struct_compute_av(policydb, scontext, tcontext, tclass, avd, xperms);
}
#endif
