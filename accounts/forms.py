from django import forms
from django.conf import settings
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from .models import Education, Experience, Profile, Skill


class SignupForm(UserCreationForm):
    first_name = forms.CharField(max_length=60)
    last_name = forms.CharField(max_length=60)
    email = forms.EmailField()

    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "email")


class ProfileForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = ("headline", "location", "about", "photo")

    def clean_photo(self):
        photo = self.cleaned_data.get("photo")
        if photo and getattr(photo, "size", 0) > settings.MAX_PHOTO_BYTES:
            raise forms.ValidationError("Photos must be 5 MB or smaller.")
        return photo


class ExperienceForm(forms.ModelForm):
    class Meta:
        model = Experience
        fields = ("title", "company", "start_date", "end_date", "description")
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
        }

    def clean(self):
        data = super().clean()
        s, e = data.get("start_date"), data.get("end_date")
        if s and e and e < s:
            self.add_error("end_date", "End date cannot be before the start date.")
        return data


class EducationForm(forms.ModelForm):
    class Meta:
        model = Education
        fields = ("school", "degree", "start_year", "end_year")


class SkillForm(forms.ModelForm):
    class Meta:
        model = Skill
        fields = ("name",)
