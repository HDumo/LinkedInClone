from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from network.models import relationship
from notifications.models import notify

from .forms import EducationForm, ExperienceForm, ProfileForm, SignupForm, SkillForm
from .models import Endorsement, Skill


@ratelimit(key="ip", rate="10/h", method="POST", block=True)
def signup(request):
    if request.user.is_authenticated:
        return redirect("home")
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect("profile_edit")
    return render(request, "registration/signup.html", {"form": form})


def profile_detail(request, username):
    owner = get_object_or_404(User.objects.select_related("profile"), username=username)
    profile = owner.profile
    skills = list(profile.skills.prefetch_related("endorsements"))
    endorsed = set()
    if request.user.is_authenticated:
        endorsed = set(
            Endorsement.objects.filter(endorser=request.user, skill__in=skills).values_list("skill_id", flat=True)
        )
    return render(request, "accounts/profile.html", {
        "owner": owner,
        "profile": profile,
        "skills": skills,
        "endorsed": endorsed,
        "rel": relationship(request.user, owner) if request.user.is_authenticated else "none",
        "is_me": request.user == owner,
    })


@login_required
def profile_edit(request):
    form = ProfileForm(request.POST or None, request.FILES or None, instance=request.user.profile)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Profile updated.")
        return redirect("profile", username=request.user.username)
    return render(request, "form.html", {"form": form, "title": "Edit profile", "multipart": True})


def _add_item(request, form_class, title):
    form = form_class(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.profile = request.user.profile
        try:
            with transaction.atomic():
                obj.save()
        except IntegrityError:  # duplicate skill
            messages.error(request, "That entry already exists.")
        return redirect("profile", username=request.user.username)
    return render(request, "form.html", {"form": form, "title": title})


@login_required
def add_experience(request):
    return _add_item(request, ExperienceForm, "Add experience")


@login_required
def add_education(request):
    return _add_item(request, EducationForm, "Add education")


@login_required
def add_skill(request):
    return _add_item(request, SkillForm, "Add skill")


@login_required
@require_POST
def endorse(request, pk):
    skill = get_object_or_404(Skill, pk=pk)
    owner = skill.profile.user
    if owner != request.user:
        _, created = Endorsement.objects.get_or_create(skill=skill, endorser=request.user)
        if created:
            notify(owner, request.user, f"endorsed you for {skill.name}", reverse("profile", args=[owner.username]))
    return redirect("profile", username=owner.username)
