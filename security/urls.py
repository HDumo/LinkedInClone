from django.urls import path

from . import portal, views

urlpatterns = [
    # Members
    path("accounts/devices/", views.signed_in_devices, name="signed_in_devices"),
    path("accounts/activity/", views.my_activity, name="my_activity"),
    path("accounts/unlock/", views.unlock_request, name="unlock_request"),
    path("accounts/unlock/code/", views.unlock_confirm, name="unlock_confirm"),
    # Staff portal
    path("security/", portal.home, name="security_home"),
    path("security/locked/", portal.locked, name="security_locked"),
    path("security/people/", portal.people, name="security_people"),
    path("security/visitors/", portal.visitors, name="security_visitors"),
    path("security/trending/", portal.trending, name="security_trending"),
    path("security/settings/", portal.settings_page, name="security_settings"),
    path("security/rules/", portal.rules_list, name="security_rules"),
    path("security/rules/new/", portal.rule_edit, name="security_rule_new"),
    path("security/rules/<int:pk>/", portal.rule_edit, name="security_rule_edit"),
    path("security/rules/<int:pk>/action/", portal.rule_action, name="security_rule_action"),
]
