from collections import defaultdict

from django.contrib.auth.models import Group, User
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone
from django.apps import apps

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveAllianceInfo, EveCorporationInfo

from . import app_settings, filter as smart_filters

if app_settings.discord_bot_active():
    import aadiscordbot

import logging

logger = logging.getLogger(__name__)


class GroupUpdateWebhook(models.Model):
    group = models.ForeignKey(Group, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=False)
    webhook = models.TextField()
    extra_message = models.TextField(default="", blank=True)

    def __str__(self):
        return "Group Update Hook for: %s" % self.group.name


class SmartFilter(models.Model):
    class Meta:
        verbose_name = "Smart Filter Binding"
        verbose_name_plural = "Smart Filter Catalog"

    content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, editable=False
    )
    object_id = models.PositiveIntegerField(editable=False)
    filter_object = GenericForeignKey("content_type", "object_id")
    grace_period = models.IntegerField(default=5)

    def __str__(self):
        try:
            return f"{self.filter_object.name}: {self.filter_object.description}"
        except:  # noqa: E722
            return f"Error: {self.content_type.app_label}:{self.content_type} {self.object_id} Not Found"


class FilterBase(models.Model):

    name = models.CharField(max_length=500)
    description = models.CharField(max_length=500)

    class Meta:
        abstract = True

    def __str__(self):
        return f"{self.name}: {self.description}"

    def process_filter(self, user: User):
        raise NotImplementedError("Please Create a filter!")

    def audit_filter(self, users):
        raise NotImplementedError("Please Create an audit function!")


class DiscordActivatedFilter(FilterBase):
    class Meta:
        verbose_name = "Smart Filter: Discord"
        verbose_name_plural = verbose_name

    negate_result = models.BooleanField(default=False)

    def process_filter(self, user: User) -> bool:
        if not apps.is_installed("allianceauth.services.modules.discord") or not app_settings.USING_DISCORD_SERVICE:
            return False
        else:
            from allianceauth.services.modules.discord.models import DiscordUser

            try:
                discord_user = DiscordUser.objects.get(user=user)
                logger.info(f"Got Discord status for {user}: {discord_user.activated is not None}")
                return discord_user.activated is None if self.negate_result else discord_user.activated is not None
            except DiscordUser.DoesNotExist:
                logger.warning(f"{user} Does not have Discord activated or Does not exist in Discord user DB")
                return False
            except Exception as e:
                logger.error(f"Error occurred when processing Discord filter: {e}")
                return False

    def audit_filter(self, users):
        output = defaultdict(lambda: {"message": "", "check": False})

        if not apps.is_installed("allianceauth.services.modules.discord"):
            for user in users:
                output[user.id] = {
                    "message": "Discord service not installed",
                    "check": False
                }
            return output

        from allianceauth.services.modules.discord.models import DiscordUser

        discord_users = DiscordUser.objects.filter(
            user__in=users
        ).select_related('user')

        activated_map = {
            du.user.id: du.activated is not None
            for du in discord_users
        }

        for user in users:
            is_active = activated_map.get(user.id, False)
            output[user.id] = {
                "message": "Activated" if is_active else "Not activated or not linked",
                "check": is_active
            }

        return output


class FilterExpression(FilterBase):
    class Meta:
        verbose_name = "Smart Filter: Expression"
        verbose_name_plural = verbose_name

    first_term = models.ForeignKey(
        SmartFilter,
        on_delete=models.CASCADE,
        related_name="+"
    )

    class OperatorChoices(models.TextChoices):
        AND = "and"
        OR = "or"
        XOR = "xor"

    operator = models.CharField(
        max_length=10,
        choices=OperatorChoices.choices,
    )

    second_term = models.ForeignKey(
        SmartFilter,
        on_delete=models.CASCADE,
        related_name="+",
    )

    negate_result = models.BooleanField(default=False)

    def process_filter(self, user: User):
        first = self.first_term.filter_object.process_filter(user)
        second = self.second_term.filter_object.process_filter(user)

        result = False

        if self.operator == self.OperatorChoices.AND:
            result = first and second
        elif self.operator == self.OperatorChoices.OR:
            result = first or second
        elif self.operator == self.OperatorChoices.XOR:
            result = first ^ second
        else:
            return False

        if self.negate_result:
            result = not result
        return result

    def audit_filter(self, users):
        first_res = self.first_term.filter_object.audit_filter(users)
        second_res = self.second_term.filter_object.audit_filter(users)

        output = defaultdict(lambda: {"message": "", "check": False})

        for user in users:
            first = first_res[user.id]
            second = second_res[user.id]

            if self.operator == self.OperatorChoices.AND:
                check = first["check"] and second["check"]
                if self.negate_result:
                    check = not check

                output[user.id] = {
                    "check": check,
                    "message": f"{'NOT ' if self.negate_result else ''}{first['message']} AND {second['message']}",
                }
            elif self.operator == self.OperatorChoices.OR:
                check = first["check"] or second["check"]
                if self.negate_result:
                    check = not check

                output[user.id] = {
                    "check": check,
                    "message": f"{'NOT ' if self.negate_result else ''}{first['message']} OR {second['message']}",
                }
            elif self.operator == self.OperatorChoices.XOR:
                check = first["check"] ^ second["check"]
                if self.negate_result:
                    check = not check

                output[user.id] = {
                    "check": check,
                    "message": f"{'NOT ' if self.negate_result else ''}{first['message']} XOR {second['message']}",
                }
            else:
                output[user.id]["check"] = False
                output[user.id]["message"] = "Invalid operator"

        return output


class AltCorpFilter(FilterBase):
    class Meta:
        verbose_name = "Smart Filter: Character in Corporation"
        verbose_name_plural = verbose_name

    alt_corp = models.ForeignKey(EveCorporationInfo, on_delete=models.CASCADE)

    # sometimes there are double standards.
    exempt_alliances = models.ManyToManyField(
        EveAllianceInfo, related_name="corp_exempt_alliances", blank=True)
    exempt_corporations = models.ManyToManyField(
        EveCorporationInfo, related_name="corp_exempt_corporations", blank=True)

    def process_filter(self, user: User):
        return smart_filters.check_alt_corp_on_account(
            user, self.alt_corp.corporation_id,
            exempt_allis=self.exempt_alliances.all().values_list("alliance_id", flat=True),
            exempt_corps=self.exempt_corporations.all().values_list("corporation_id", flat=True)
        )

    def audit_filter(self, users):
        co = CharacterOwnership.objects.filter(user__in=users, character__corporation_id=self.alt_corp.corporation_id).values(
            'user__id', 'character__character_name')

        chars = defaultdict(list)
        for c in co:
            chars[c['user__id']].append(c['character__character_name'])

        output = defaultdict(lambda: {"message": "", "check": False})
        for c, char_list in chars.items():
            output[c] = {"message": ", ".join(char_list), "check": True}
        return output


class AltAllianceFilter(FilterBase):
    class Meta:
        verbose_name = "Smart Filter: Character in Alliance"
        verbose_name_plural = verbose_name

    alt_alli = models.ForeignKey(EveAllianceInfo, on_delete=models.CASCADE)

    # sometimes there are double standards.
    exempt_alliances = models.ManyToManyField(
        EveAllianceInfo, related_name="alli_exempt_alliances", blank=True)
    exempt_corporations = models.ManyToManyField(
        EveCorporationInfo, related_name="alli_exempt_corporations", blank=True)

    def process_filter(self, user: User):
        return smart_filters.check_alt_alli_on_account(user, self.alt_alli.alliance_id,
                                                       exempt_allis=self.exempt_alliances.all().values_list("alliance_id", flat=True),
                                                       exempt_corps=self.exempt_corporations.all().values_list("corporation_id", flat=True)
                                                       )

    def audit_filter(self, users):
        co = CharacterOwnership.objects.filter(user__in=users, character__alliance_id=self.alt_alli.alliance_id).values(
            'user__id', 'character__character_name')
        chars = defaultdict(list)
        for c in co:
            chars[c['user__id']].append(c['character__character_name'])

        output = defaultdict(lambda: {"message": "", "check": False})
        for c, char_list in chars.items():
            output[c] = {"message": ", ".join(char_list), "check": True}
        return output


class UserInGroupFilter(FilterBase):
    class Meta:
        verbose_name = "Smart Filter: User Has Group"
        verbose_name_plural = verbose_name

    groups = models.ManyToManyField(Group, related_name="in_group")

    # sometimes there are double standards.
    exempt_alliances = models.ManyToManyField(
        EveAllianceInfo, related_name="group_exempt_alliances", blank=True)
    exempt_corporations = models.ManyToManyField(
        EveCorporationInfo, related_name="group_exempt_corporations", blank=True)

    reversed_logic = models.BooleanField(default=False)

    def process_filter(self, user: User):
        return smart_filters.check_group_on_account(user, self.groups.all(),
                                                    exempt_allis=self.exempt_alliances.all().values_list("alliance_id", flat=True),
                                                    exempt_corps=self.exempt_corporations.all().values_list("corporation_id", flat=True)
                                                    )

    def audit_filter(self, users):
        cl = users.prefetch_related('groups').filter(
            groups__id__in=self.groups.all().values_list("id", flat=True))
        chars = defaultdict(
            lambda: {"message": "", "check": self.reversed_logic})
        for c in cl:
            chars[c.id] = {"message": "", "check": not self.reversed_logic}
        return chars


class SmartGroup(models.Model):
    group = models.OneToOneField(Group, on_delete=models.CASCADE)
    description = models.CharField(max_length=500, default="", blank=True)
    filters = models.ManyToManyField(SmartFilter)
    last_update = models.DateTimeField(auto_now=True)

    auto_group = models.BooleanField(default=False)
    enabled = models.BooleanField(default=True)
    include_in_updates = models.BooleanField(default=True)

    can_grace = models.BooleanField(default=False)

    notify_on_remove = models.BooleanField(default=True)
    notify_on_grace = models.BooleanField(default=True)
    notify_on_add = models.BooleanField(default=False)

    class Meta:
        permissions = (
            ("access_sec_group", "Can access sec group requests screen."),
            ("audit_sec_group", "Can audit sec groups members."),)

    def __str__(self):
        return "Smart Group: %s" % self.group.name

    def run_checks(self, user: User):
        output = []
        for check in self.filters.all():
            try:
                _filter = check.filter_object
                if _filter is None:
                    logger.warning(f"Failed to run filter for {check}")
                    continue  # Skip as this is broken...
                test_pass = _filter.process_filter(user)
            except:  # noqa: E722
                test_pass = False
                logger.error("TEST FAILED")  # TODO Make pretty
            _check = {
                "name": check.filter_object.description,
            }
            _check["check"] = test_pass
            _check["filter"] = check
            output.append(_check)
        return output

    def run_check_on_user(self, user: User):
        output = []
        for check in self.filters.all():
            _filter = check.filter_object
            if _filter is None:
                logger.warning(f"Failed to run filter for {check}")
                continue  # Skip as this is broken...
            try:
                test_pass = _filter.audit_filter(
                    User.objects.filter(pk=user.pk)
                )
            except Exception as e:
                try:
                    print(e)
                    test_pass = {
                        user.id: {
                            "message": "",
                            "check": _filter.process_filter(user)
                        }
                    }
                except Exception:
                    test_pass = {
                        user.id: {
                            "message": "Filter Failed",
                            "check": False
                        }
                    }
                    logger.error("TEST FAILED")  # TODO Make pretty
            _check = {
                "name": check.filter_object.description,
            }
            _check["check"] = test_pass[user.id]['check']
            _check["message"] = test_pass[user.id]['message']
            _check["filter"] = check
            output.append(_check)
        return output

    def process_checks(self, checks):
        out = True
        for c in checks:
            out = out & c.get("check", False)
        return out

    def check_user(self, user: User):
        checks = self.run_check_on_user(user)
        out = self.process_checks(checks)
        return out


class GracePeriodRecord(models.Model):
    group = models.ForeignKey(SmartGroup, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    grace_filter = models.ForeignKey(SmartFilter, on_delete=models.CASCADE)
    expires = models.DateTimeField()

    def __str__(self):
        return "{} - {} - {}".format(self.user, self.group, self.grace_filter)

    def is_expired(self):
        return self.expires < timezone.now()

    def notify_user(self, message):
        # dm user if has discord account and discord bot installed
        if app_settings.discord_bot_active():
            try:
                aadiscordbot.tasks.send_direct_message.delay(
                    self.user.discord.uid, message
                )
            except Exception as e:
                logger.error(e, exc_info=True)
                pass
        else:
            pass


class PendingNotification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    filter = models.ForeignKey(SmartFilter, on_delete=models.CASCADE)
    group = models.ForeignKey(SmartGroup, on_delete=models.CASCADE)

    message = models.TextField(blank=True, default=None, null=True)

    removal = models.BooleanField(default=False)

    notified = models.BooleanField(default=False)

    @classmethod
    def get_grace_notifications(cls):
        notifications = cls.objects.filter(notified=False, removal=False)
        out = defaultdict(list)
        for n in notifications:
            out[n.user].append(n)

        return out, notifications

    @classmethod
    def get_kick_notifications(cls):
        notifications = cls.objects.filter(notified=False, removal=True)
        out = defaultdict(list)
        for n in notifications:
            out[n.user].append(n)

        return out, notifications