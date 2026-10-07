from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from notifications.models import notify

from .models import Application, Company, Job


class CompanyForm(forms.ModelForm):
    class Meta:
        model = Company
        fields = ("name", "description", "website")


class JobForm(forms.ModelForm):
    class Meta:
        model = Job
        fields = ("title", "company", "location", "description")


@login_required
def job_list(request):
    q = request.GET.get("q", "").strip()
    jobs = Job.objects.select_related("company")
    if q:
        jobs = jobs.filter(Q(title__icontains=q) | Q(description__icontains=q) | Q(location__icontains=q) | Q(company__name__icontains=q))
    return render(request, "jobs/list.html", {"jobs": jobs[:50], "q": q})


@login_required
def job_create(request):
    form = JobForm(request.POST or None)
    form.fields["company"].queryset = Company.objects.filter(owner=request.user)
    if request.method == "POST" and form.is_valid():
        job = form.save(commit=False)
        job.posted_by = request.user
        job.save()
        return redirect("job_detail", pk=job.pk)
    return render(request, "form.html", {"form": form, "title": "Post a job"})


@login_required
def job_detail(request, pk):
    job = get_object_or_404(Job.objects.select_related("company", "posted_by"), pk=pk)
    is_owner = job.posted_by == request.user
    return render(request, "jobs/detail.html", {
        "job": job,
        "is_owner": is_owner,
        "applied": job.applications.filter(applicant=request.user).exists(),
        "applications": job.applications.select_related("applicant__profile") if is_owner else [],
    })


@login_required
@require_POST
def job_apply(request, pk):
    job = get_object_or_404(Job, pk=pk)
    if job.posted_by == request.user:
        return HttpResponseForbidden("You cannot apply to your own job.")
    _, created = Application.objects.get_or_create(
        job=job, applicant=request.user, defaults={"cover_letter": request.POST.get("cover_letter", "")[:3000]}
    )
    if created:
        notify(job.posted_by, request.user, f"applied to {job.title}", reverse("job_detail", args=[job.pk]))
        messages.success(request, "Application sent.")
    else:
        messages.info(request, "You already applied.")
    return redirect("job_detail", pk=job.pk)


@login_required
def my_applications(request):
    return render(request, "jobs/applications.html", {"applications": request.user.applications.select_related("job__company")})


@login_required
def company_list(request):
    return render(request, "jobs/company_list.html", {"companies": Company.objects.all()})


@login_required
def company_create(request):
    form = CompanyForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        company = form.save(commit=False)
        company.owner = request.user
        company.save()
        return redirect("company_detail", slug=company.slug)
    return render(request, "form.html", {"form": form, "title": "Create company page"})


@login_required
def company_detail(request, slug):
    company = get_object_or_404(Company, slug=slug)
    return render(request, "jobs/company_detail.html", {"company": company, "jobs": company.jobs.all()})
