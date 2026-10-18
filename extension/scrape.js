// Injected into the job posting tab. The value of the last expression is returned to the popup.
(() => {
  const q = (s) => document.querySelector(s);
  const textOf = (el) => (el ? (el.innerText || el.textContent || "").trim() : "");
  const first = (...sels) => {
    for (const s of sels) {
      const t = textOf(q(s));
      if (t) return t.replace(/\s+\n/g, "\n");
    }
    return "";
  };
  const meta = (n) => (q(`meta[property="${n}"]`) || q(`meta[name="${n}"]`))?.content?.trim() || "";
  const htmlToText = (html) => {
    if (!html) return "";
    if (!html.includes("<") && html.includes("&lt;")) {
      html = new DOMParser().parseFromString(html, "text/html").documentElement.textContent;
    }
    const marked = html
      .replace(/<li[^>]*>/gi, "\n• ")
      .replace(/<\/?(br|p|div|h\d|ul|ol)\b[^>]*>|<\/li>/gi, "\n");
    const text = new DOMParser().parseFromString(marked, "text/html").documentElement.textContent;
    return text.split("\n").map((l) => l.replace(/\s+/g, " ").trim()).join("\n").replace(/\n{3,}/g, "\n\n").trim();
  };

  const host = location.hostname.replace(/^www\./, "");
  const job = { url: location.href, title: "", company: "", location: "", salary: "", description: "", source: host };

  // 1. schema.org JobPosting — most ATS pages (Greenhouse, Lever, Ashby, SmartRecruiters, many career sites) publish it.
  const findPosting = (node) => {
    if (Array.isArray(node)) {
      for (const n of node) { const f = findPosting(n); if (f) return f; }
    } else if (node && typeof node === "object") {
      const t = node["@type"];
      if (t === "JobPosting" || (Array.isArray(t) && t.includes("JobPosting"))) return node;
      if (node["@graph"]) return findPosting(node["@graph"]);
    }
    return null;
  };
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    let jp;
    try { jp = findPosting(JSON.parse(s.textContent)); } catch { continue; }
    if (!jp) continue;
    const org = jp.hiringOrganization;
    job.title = String(jp.title || "").trim();
    job.company = (typeof org === "string" ? org : org?.name) || "";
    const locs = [].concat(jp.jobLocation || []);
    const places = locs.map((l) => {
      const a = l?.address || {};
      if (typeof a === "string") return a;
      const country = typeof a.addressCountry === "object" ? a.addressCountry?.name : a.addressCountry;
      return [a.addressLocality, a.addressRegion, country].filter(Boolean).join(", ");
    });
    if (String(jp.jobLocationType || "").includes("TELECOMMUTE")) places.unshift("Remote");
    job.location = [...new Set(places.filter(Boolean))].join("; ");
    const bs = jp.baseSalary;
    if (bs && typeof bs === "object") {
      const v = bs.value;
      const fmt = (x) => (isNaN(Number(x)) ? String(x) : Number(x).toLocaleString("en-US"));
      const lo = v?.minValue, hi = v?.maxValue, one = typeof v === "object" ? v?.value : v;
      const amount = lo && hi && lo !== hi ? `${fmt(lo)}–${fmt(hi)}` : (lo || hi || one) ? fmt(lo || hi || one) : "";
      if (amount) job.salary = [bs.currency, amount, v?.unitText ? `/ ${String(v.unitText).toLowerCase()}` : ""].filter(Boolean).join(" ");
    }
    job.description = htmlToText(jp.description || "");
    break;
  }

  // 2. Site-specific pages that render client-side (selectors change; fallbacks below catch the rest).
  if (host.endsWith("linkedin.com")) {
    const id = new URLSearchParams(location.search).get("currentJobId") || (location.pathname.match(/\/jobs\/view\/(?:[^/]*-)?(\d+)/) || [])[1];
    if (id) job.url = `https://www.linkedin.com/jobs/view/${id}/`;
    job.title = first(".job-details-jobs-unified-top-card__job-title", ".jobs-unified-top-card__job-title", ".top-card-layout__title", "h1") || job.title;
    job.company = first(".job-details-jobs-unified-top-card__company-name", ".jobs-unified-top-card__company-name", ".topcard__org-name-link") || job.company;
    job.location = job.location || first(
      ".job-details-jobs-unified-top-card__primary-description-container .tvm__text",
      ".job-details-jobs-unified-top-card__bullet", ".topcard__flavor--bullet");
    job.description = first("#job-details", ".jobs-description__content", ".show-more-less-html__markup") || job.description;
    const parts = document.title.replace(/^\(\d+\)\s*/, "").split(" | ");
    if (parts.length >= 3 && parts.at(-1) === "LinkedIn") {
      if (!job.company || job.title === document.title) job.company = parts[1];
      if (!job.title || job.title.includes(" | ")) job.title = parts[0];
    }
  } else if (host.includes("indeed.")) {
    job.title = (first('[data-testid="jobsearch-JobInfoHeader-title"]', "h1.jobsearch-JobInfoHeader-title", "h1") || job.title).replace(/\n- job post$/i, "");
    job.company = first('[data-testid="inlineHeader-companyName"]', '[data-company-name="true"]') || job.company;
    job.location = first('[data-testid="inlineHeader-companyLocation"]', '[data-testid="job-location"]') || job.location;
    job.salary = job.salary || first("#salaryInfoAndJobType");
    job.description = first("#jobDescriptionText") || job.description;
  } else if (host.includes("myworkdayjobs.com") || host.includes("workday")) {
    job.title = first('[data-automation-id="jobPostingHeader"]') || job.title;
    job.location = first('[data-automation-id="locations"] dd', '[data-automation-id="locations"]') || job.location;
    job.description = first('[data-automation-id="jobPostingDescription"]') || job.description;
    job.company = job.company || host.split(".")[0];
  } else if (host.includes("lever.co")) {
    job.title = job.title || first(".posting-headline h2");
    job.location = job.location || first(".posting-categories .location");
    job.company = job.company || location.pathname.split("/")[1] || "";
    job.description = job.description || first('[data-qa="job-description"]', ".content .section-wrapper");
  } else if (host.includes("greenhouse.io")) {
    job.title = job.title || first(".job__title h1", "h1.app-title", "h1");
    job.company = job.company || first(".company-name") || location.pathname.split("/")[1] || "";
    job.location = job.location || first(".job__location", ".location");
    job.description = job.description || first(".job__description", "#content");
  } else if (host.includes("glassdoor.")) {
    job.title = job.title || first('[data-test="job-title"]', "h1");
    job.company = job.company || first('[data-test="employer-name"]');
    job.location = job.location || first('[data-test="location"]');
    job.description = job.description || first('[class*="JobDetails_jobDescription"]', ".jobDescriptionContent");
  }

  // 3. Generic fallbacks.
  job.title = job.title || meta("og:title") || first("h1") || document.title;
  job.company = job.company || meta("og:site_name");
  if (!job.description) {
    const candidates = [...document.querySelectorAll('[class*="description" i], [id*="description" i], [class*="job-details" i]')]
      .map(textOf).filter((t) => t.length > 300).sort((a, b) => b.length - a.length);
    job.description = candidates[0] || "";
  }
  // Text the user highlighted always wins — the escape hatch for unusual pages.
  const selection = String(window.getSelection() || "").trim();
  if (selection.length > 150) job.description = selection;

  for (const k of Object.keys(job)) job[k] = String(job[k] || "").trim().slice(0, k === "description" ? 50000 : 500);
  return job;
})();
