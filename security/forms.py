from django import forms

from . import rules
from .models import SecurityConfig, SecurityRule


class SecurityConfigForm(forms.ModelForm):
    class Meta:
        model = SecurityConfig
        fields = ("password_reuse_block", "login_history_days", "tracking_enabled", "tracking_days",
                  "trending_for_staff", "whitelist")
        widgets = {"whitelist": forms.Textarea(attrs={"rows": 6})}

    def clean_whitelist(self):
        text = self.cleaned_data.get("whitelist", "")
        _, bad = rules.parse_whitelist(text)
        if bad:
            raise forms.ValidationError("These lines are not IP addresses or ranges: " + ", ".join(bad[:5]))
        return text


class RuleForm(forms.Form):
    """A rule as a plain form: tick the conditions you want and fill in their numbers."""

    name = forms.CharField(max_length=80)
    enabled = forms.BooleanField(required=False, initial=True)
    match = forms.ChoiceField(choices=[(SecurityRule.ALL, "All ticked conditions must be true (AND)"),
                                       (SecurityRule.ANY, "Any one ticked condition is enough (OR)")])
    action = forms.ChoiceField(choices=SecurityRule.ACTION_CHOICES)

    PARAMS = {"countries": forms.CharField, "count": forms.IntegerField, "minutes": forms.IntegerField, "text": forms.CharField}

    def __init__(self, *args, rule=None, **kwargs):
        super().__init__(*args, **kwargs)
        by_type = {c.get("type"): c for c in (rule.conditions if rule else [])}
        if rule:
            self.initial.update(name=rule.name, enabled=rule.enabled, match=rule.match, action=rule.action)
        self.groups = []
        for kind, (label, params) in rules.CONDITIONS.items():
            existing = by_type.get(kind)
            use = forms.BooleanField(required=False, label=label)
            self.fields[f"use_{kind}"] = use
            self.initial[f"use_{kind}"] = existing is not None
            fields = [self[f"use_{kind}"]]
            for p in params:
                f = self.PARAMS[p](required=False, label={"countries": "Country codes (e.g. US, CA)", "count": "How many",
                                                          "minutes": "Within minutes", "text": "Text"}[p])
                self.fields[f"{kind}_{p}"] = f
                if existing and p in existing:
                    self.initial[f"{kind}_{p}"] = ", ".join(existing[p]) if p == "countries" else existing[p]
                fields.append(self[f"{kind}_{p}"])
            self.groups.append((kind, fields))
        self.fields["name"].initial = None

    def clean(self):
        data = super().clean()
        conditions = []
        for kind, (_, params) in rules.CONDITIONS.items():
            if data.get(f"use_{kind}"):
                conditions.append({"type": kind, **{p: data.get(f"{kind}_{p}") for p in params}})
        fields, error = rules.clean_rule({"name": data.get("name"), "match": data.get("match"), "action": data.get("action"),
                                          "enabled": data.get("enabled"), "conditions": conditions})
        if error:
            raise forms.ValidationError(error)
        self.rule_fields = fields
        return data
