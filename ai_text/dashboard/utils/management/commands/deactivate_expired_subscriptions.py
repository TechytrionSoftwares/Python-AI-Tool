from django.core.management.base import BaseCommand
from django.utils import timezone
from utils.models import UserSubscription

class Command(BaseCommand):
    help = 'Deactivates subscriptions that have expired'

    def handle(self, *args, **options):
        now = timezone.now()
        
        # Find subscriptions that are past their expiration date
        expired_subscriptions = UserSubscription.objects.filter(
            active=True,
            cancel_at_period_end=True,
            expires_at__lte=now
        )
        
        count = expired_subscriptions.count()
        
        if count > 0:
            for sub in expired_subscriptions:
                sub.active = False
                sub.save(update_fields=['active'])
                self.stdout.write(
                    self.style.SUCCESS(
                        f'✓ Deactivated subscription for user: {sub.user.username}'
                    )
                )
            
            self.stdout.write(
                self.style.SUCCESS(
                    f'Successfully deactivated {count} expired subscription(s)'
                )
            )
        else:
            self.stdout.write(self.style.WARNING('No expired subscriptions found'))