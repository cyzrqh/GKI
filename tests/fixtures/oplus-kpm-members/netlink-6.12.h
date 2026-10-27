struct netlink_kernel_cfg {
	unsigned int	groups;
	unsigned int	flags;
	void		(*input)(struct sk_buff *skb);
	int		(*bind)(struct net *net, int group);
	void		(*unbind)(struct net *net, int group);
	void            (*release) (struct sock *sk, unsigned long *groups);
};
