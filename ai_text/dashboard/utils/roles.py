def is_admin(user):
    return user.is_superuser or user.groups.filter(name="admin").exists()

def is_subscriber(user):
    return user.groups.filter(name="subscriber").exists()
