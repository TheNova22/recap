const $ = id => document.getElementById(id);
const csrf = document.querySelector('meta[name="recap-csrf"]').content;
const demoMode = document.querySelector('meta[name="recap-demo"]')?.content === "true";

let reportId = "";
let posted = [];
let configuredModel = "";
let availableModels = [];
let themePinned = Boolean(savedTheme());
const CUSTOM_MODEL = "__recap_custom_model__";
const EXCLUDE_FROM_DAILY = "__exclude_from_daily_update__";
let dailyFormat = "markdown";
let dailyFormats = { markdown: "", plain: "" };
let dailyEdited = { markdown: false, plain: false };
let settingsMode = "form";
let settingsValues = {};

function savedTheme() {
  try {
    const saved = localStorage.getItem("recap-theme");
    return saved === "light" || saved === "dark" ? saved : "";
  } catch {
    return "";
  }
}

function currentTheme() {
  return document.documentElement.dataset.theme
    || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}

function updateThemeControl() {
  const dark = currentTheme() === "dark";
  $("themeLabel").textContent = dark ? "Light mode" : "Dark mode";
  $("themeIcon").textContent = dark ? "☼" : "◐";
  $("themeToggle").setAttribute("aria-label", `Switch to ${dark ? "light" : "dark"} theme`);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = dark ? "#14211e" : "#e6ece7";
}

function populateModels(models, selected = configuredModel) {
  const select = $("model");
  const customInput = $("customModel");
  const ids = [...new Set([...models, configuredModel].filter(Boolean))];
  select.replaceChildren();
  if (!ids.length) {
    const empty = element("option", "Choose a model");
    empty.value = "";
    empty.disabled = true;
    select.append(empty);
  }
  for (const id of ids) {
    const suffix = id === configuredModel ? " · configured" : "";
    const option = element("option", `${id}${suffix}`);
    option.value = id;
    select.append(option);
  }
  const customOption = element("option", "Enter a custom model ID…");
  customOption.value = CUSTOM_MODEL;
  select.append(customOption);

  if (selected && ids.includes(selected)) {
    select.value = selected;
    customInput.value = "";
  } else if (selected) {
    select.value = CUSTOM_MODEL;
    customInput.value = selected;
  } else {
    select.value = ids.length ? ids[0] : "";
    customInput.value = "";
  }
  customInput.hidden = select.value !== CUSTOM_MODEL;
}

function selectedModel() {
  return $("model").value === CUSTOM_MODEL
    ? $("customModel").value.trim()
    : $("model").value || configuredModel;
}

function convertDailyText(text, fromFormat, toFormat) {
  if (fromFormat === toFormat) return text;
  const lines = text.split("\n");
  if (fromFormat === "markdown") {
    if (lines[0]?.startsWith("# ")) lines[0] = lines[0].slice(2);
    return lines.map(line => line.startsWith("- ") ? line.slice(2) : line).join("\n");
  }

  const headingIndex = lines.findIndex(line => line.trim());
  if (headingIndex >= 0 && !lines[headingIndex].startsWith("# ")) {
    lines[headingIndex] = `# ${lines[headingIndex]}`;
  }
  for (let index = headingIndex + 1; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.trim() && line !== "No activity found for this date." && !line.startsWith("- ")) {
      lines[index] = `- ${line}`;
    }
  }
  return lines.join("\n");
}

$("themeToggle").addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  themePinned = true;
  try {
    localStorage.setItem("recap-theme", next);
  } catch {
    // The theme still applies for this tab when storage is unavailable.
  }
  updateThemeControl();
});

$("openSettings").addEventListener("click", () => {
  const panel = document.querySelector(".settings-panel");
  const summary = panel.querySelector("summary");
  panel.open = true;
  summary.focus({ preventScroll: true });
  panel.scrollIntoView({
    behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
    block: "start",
  });
});

window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", event => {
  if (!themePinned) {
    document.documentElement.dataset.theme = event.matches ? "dark" : "light";
    updateThemeControl();
  }
});
updateThemeControl();

$("model").addEventListener("change", () => {
  const custom = $("model").value === CUSTOM_MODEL;
  $("customModel").hidden = !custom;
  if (custom) $("customModel").focus();
});

async function api(path, body) {
  const options = {
    method: body === undefined ? "GET" : "POST",
    headers: { "X-Recap-Nonce": csrf },
    cache: "no-store",
  };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function status(message, state = "") {
  const node = $("status");
  node.textContent = message;
  if (state) node.dataset.state = state;
  else delete node.dataset.state;
}

function element(tag, value, className) {
  const node = document.createElement(tag);
  if (value !== undefined) node.textContent = value;
  if (className) node.className = className;
  return node;
}

function setSettingsFeedback(message, state = "") {
  const feedback = $("settingsFeedback");
  feedback.textContent = message;
  feedback.hidden = !message;
  if (state) feedback.dataset.state = state;
  else delete feedback.dataset.state;
}

function resizeRepoRoots() {
  const field = $("settingRepoRoots");
  field.style.height = "auto";
  field.style.height = `${Math.min(Math.max(field.scrollHeight + 2, 42), 180)}px`;
}

function populateSettingsForm(values) {
  settingsValues = { ...values, templates: { ...(values.templates || {}) }, custom: { ...(values.custom || {}) } };
  $("settingRepoRoots").value = (values.repo_roots || []).join("\n");
  resizeRepoRoots();
  $("settingRepoExcludes").value = (values.repo_excludes || []).join(", ");
  $("settingTimezone").value = values.timezone || "local";
  $("settingJiraUrl").value = values.jira_base_url || "";
  $("settingJiraDays").value = values.jira_recent_days || 30;
  const aiProviderSelect = $("settingAIProvider");
  const aiProvider = values.ai_provider || "openai_responses";
  if (![...aiProviderSelect.options].some(option => option.value === aiProvider)) {
    aiProviderSelect.append(element("option", aiProvider));
    aiProviderSelect.lastElementChild.value = aiProvider;
  }
  aiProviderSelect.value = aiProvider;
  $("settingAIBaseUrl").value = values.ai_base_url || "https://api.openai.com/v1";
    $("settingAIModel").value = values.ai_model || "";
  $("settingAITokens").value = values.ai_max_output_tokens || 4096;
  const templateSelect = $("settingCommentTemplate");
  templateSelect.replaceChildren();
  for (const name of Object.keys(settingsValues.templates)) templateSelect.append(element("option", name));
  templateSelect.value = values.comment_template || Object.keys(settingsValues.templates)[0] || "concise";
  for (const checkbox of document.querySelectorAll('input[name="agentSource"]')) {
    checkbox.checked = (values.agent_sources || []).includes(checkbox.value);
  }
}

function readSettingsForm() {
  return {
    ...settingsValues,
    repo_roots: $("settingRepoRoots").value.split("\n").map(value => value.trim()).filter(Boolean),
    repo_excludes: $("settingRepoExcludes").value.split(/[\n,]/).map(value => value.trim()).filter(Boolean),
    agent_sources: [...document.querySelectorAll('input[name="agentSource"]:checked')].map(input => input.value),
    timezone: $("settingTimezone").value.trim(),
    jira_base_url: $("settingJiraUrl").value.trim(),
    jira_recent_days: Number($("settingJiraDays").value),
    ai_provider: $("settingAIProvider").value,
    ai_base_url: $("settingAIBaseUrl").value.trim(),
    ai_model: $("settingAIModel").value.trim(),
    ai_max_output_tokens: Number($("settingAITokens").value),
    comment_template: $("settingCommentTemplate").value,
  };
}

function showSettingsMode(mode) {
  settingsMode = mode;
  const formMode = mode === "form";
  $("settingsForm").hidden = !formMode;
  $("settingsFile").hidden = formMode;
  $("settingsFormMode").setAttribute("aria-pressed", String(formMode));
  $("settingsFileMode").setAttribute("aria-pressed", String(!formMode));
  try { localStorage.setItem("recap-settings-mode", mode); } catch {}
}

async function switchSettingsMode(mode) {
  if (mode === settingsMode) return;
  const controls = [$("settingsFormMode"), $("settingsFileMode")];
  controls.forEach(button => { button.disabled = true; });
  setSettingsFeedback("");
  try {
    const converted = mode === "file"
      ? await api("/api/settings/convert", { values: readSettingsForm() })
      : await api("/api/settings/convert", { text: $("configText").value });
    $("configText").value = converted.text;
    populateSettingsForm(converted.values);
    showSettingsMode(mode);
  } catch (error) {
    setSettingsFeedback(error.message, "error");
    status(error.message, "error");
  } finally {
    controls.forEach(button => { button.disabled = false; });
  }
}

function validateFormTimezone(focusInvalid = false) {
  const field = $("settingTimezone");
  const timezone = field.value.trim();
  if (timezone.toLowerCase() === "local") {
    field.removeAttribute("aria-invalid");
    return;
  }
  try {
    new Intl.DateTimeFormat("en", { timeZone: timezone });
    field.removeAttribute("aria-invalid");
  } catch {
    field.setAttribute("aria-invalid", "true");
    if (focusInvalid) field.focus();
    throw new Error(`Unknown time zone “${timezone}”. Use an IANA name such as Asia/Kolkata, or local.`);
  }
}

$("settingsFormMode").addEventListener("click", () => switchSettingsMode("form"));
$("settingsFileMode").addEventListener("click", () => switchSettingsMode("file"));
$("settingRepoRoots").addEventListener("input", resizeRepoRoots);
$("settingTimezone").addEventListener("input", () => {
  $("settingTimezone").removeAttribute("aria-invalid");
  setSettingsFeedback("");
});
$("settingTimezone").addEventListener("blur", () => {
  try {
    validateFormTimezone();
  } catch (error) {
    setSettingsFeedback(error.message, "error");
  }
});

function sourceName(source) {
  const names = {
    github: "GitHub",
    local_git: "Local git",
    agent: "Agent session",
    agent_session: "Agent session",
    agent_sessions: "Agent session",
    sessions: "Agent session",
    codex: "Codex",
    claude: "Claude Code",
    cursor: "Cursor",
  };
  return names[source] || source.replaceAll("_", " ").replace(/^./, letter => letter.toUpperCase());
}

function render(data) {
  const report = data.report;
  reportId = data.report_id;
  posted = data.posted || [];

  $("result").hidden = false;
  $("dailyPanel").hidden = false;
  $("commentsPanel").hidden = false;
  $("activityCount").textContent = `${report.items.length} ${report.items.length === 1 ? "item" : "items"} · ${report.date} · ${report.timezone}`;

  const warnings = $("warnings");
  warnings.replaceChildren();
  for (const warning of report.warnings || []) warnings.append(element("div", warning, "warning"));

  const items = $("items");
  items.replaceChildren();
  if (!report.items.length) {
    items.append(element("p", "No activity found for this date. Try another day or check your configured repository folders.", "empty-activity"));
  }
  for (const item of report.items) {
    const matched = Boolean(item.issue_key);
    const row = element("article", undefined, "activity-item");
    const copy = element("div", undefined, "activity-copy");
    const meta = element("div", undefined, "activity-meta");
    const source = element("span", sourceName(item.source), "source-tag");
    source.dataset.source = item.source;
    meta.append(source);
    if (item.at) meta.append(element("span", item.at, "activity-time"));
    copy.append(meta, element("p", item.summary || item.title || "Work item", "activity-summary"));
    if (item.url) {
      const link = element("a", "Open source item", "activity-link");
      link.href = item.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      copy.append(link);
    }

    const assignment = element("div", undefined, "assignment");
    assignment.dataset.matched = String(matched);
    assignment.dataset.excluded = String(item.include_in_daily_update === false);
    const label = element("label", "Destination");
    const select = document.createElement("select");
    select.setAttribute("aria-label", `Destination for ${item.summary || item.title || "work item"}`);
    const none = element("option", "Daily update only");
    none.value = "";
    select.append(none);
    const exclude = element("option", "None — exclude from daily update");
    exclude.value = EXCLUDE_FROM_DAILY;
    select.append(exclude);
    for (const issue of report.issues) {
      const option = element("option", `${issue.key}: ${issue.title}`);
      option.value = issue.key;
      select.append(option);
    }
    select.value = item.include_in_daily_update === false ? EXCLUDE_FROM_DAILY : item.issue_key || "";
    select.addEventListener("change", async () => {
      select.disabled = true;
      status("Updating the Jira match…");
      try {
        const updated = await api("/api/reassign", {
          report_id: reportId,
          item_id: item.id,
          issue_key: select.value === EXCLUDE_FROM_DAILY ? "" : select.value,
          include_in_daily_update: select.value !== EXCLUDE_FROM_DAILY,
        });
        render(updated);
        status("Jira match updated.", "success");
      } catch (error) {
        select.disabled = false;
        status(error.message, "error");
      }
    });
    label.append(select);
    assignment.append(label);
    row.append(copy, assignment);
    items.append(row);
  }

  dailyFormats = {
    markdown: report.daily_text,
    plain: report.daily_plain_text || report.daily_text,
  };
  dailyEdited = { markdown: false, plain: false };
  $("dailyFormat").value = dailyFormat;
  $("daily").value = dailyFormats[dailyFormat];
  const comments = $("comments");
  comments.replaceChildren();
  const keys = Object.keys(report.comments || {});
  if (!keys.length) comments.append(element("p", "No Jira comment drafts for this day.", "empty-comments"));

  for (const key of keys) {
    const card = element("article", undefined, "comment");
    const heading = element("h3");
    heading.append(element("span", key, "issue-key"));
    const issue = report.issues.find(candidate => candidate.key === key);
    if (issue) heading.append(document.createTextNode(issue.title));
    const area = document.createElement("textarea");
    area.value = report.comments[key];
    area.setAttribute("aria-label", `Comment draft for ${key}`);

    const actions = element("div", undefined, "comment-actions");
    const copyButton = element("button", `Copy ${key} comment`, "button button-quiet");
    copyButton.type = "button";
    copyButton.addEventListener("click", async () => {
 if (demoMode) {
 status("Demo mode: clipboard copy simulated.", "success");
 return;
 }
      try {
        await navigator.clipboard.writeText(area.value);
        status(`${key} comment copied.`, "success");
      } catch (error) {
        status(`Clipboard access failed: ${error.message}`, "error");
      }
    });

    const approval = element("section", undefined, "approval-panel");
    approval.hidden = true;
    approval.setAttribute("role", "group");
    approval.setAttribute("aria-labelledby", `approval-title-${key}`);
    const approvalTitle = element("h4", `Review comment for ${key}`, "approval-title");
    approvalTitle.id = `approval-title-${key}`;
    approvalTitle.tabIndex = -1;
    const approvalCopy = element("p", `This exact text will be posted to ${key}.`, "approval-copy");
    const approvalPreview = element("pre", "", "approval-preview");
    const approvalActions = element("div", undefined, "approval-actions");
    const cancelButton = element("button", "Cancel", "button button-quiet");
    cancelButton.type = "button";
    const postButton = element("button", `Post comment to ${key}`, "button button-primary");
    postButton.type = "button";
    postButton.disabled = posted.includes(key);
    approvalActions.append(cancelButton, postButton);
    approval.append(approvalTitle, approvalCopy, approvalPreview, approvalActions);

    const reviewButton = element("button", posted.includes(key) ? `Posted to ${key}` : "Review before posting", "button button-primary");
    reviewButton.type = "button";
    reviewButton.disabled = posted.includes(key);
    reviewButton.addEventListener("click", () => {
      if (!area.value.trim()) {
        status("The comment is empty.", "error");
        area.focus();
        return;
      }
      approvalPreview.textContent = area.value;
      approval.hidden = false;
      approvalTitle.focus();
      status(`Review the exact comment before posting to ${key}.`);
    });
    area.addEventListener("input", () => {
      if (!approval.hidden) {
        approvalPreview.textContent = area.value;
        postButton.disabled = posted.includes(key) || !area.value.trim();
      }
    });
    cancelButton.addEventListener("click", () => {
      approval.hidden = true;
      reviewButton.focus();
      status("Posting cancelled.");
    });
    postButton.addEventListener("click", async () => {
      if (!area.value.trim()) {
        status("The comment is empty.", "error");
        area.focus();
        return;
      }
      postButton.disabled = true;
      cancelButton.disabled = true;
      reviewButton.disabled = true;
 status(demoMode ? `Simulating comment for ${key}…` : `Posting comment to ${key}…`);
      try {
 const result = await api("/api/post", {
          report_id: reportId,
          issue_key: key,
          text: area.value,
          approved: true,
        });
        posted.push(key);
 const postedLabel = result.simulated ? `Simulated for ${key}` : `Posted to ${key}`;
 postButton.textContent = postedLabel;
        postButton.className = "posted-note";
 approvalCopy.textContent = result.simulated ? `Demo comment recorded for ${key}.` : `Posted to ${key}.`;
        approvalActions.replaceChildren(postButton);
 reviewButton.textContent = postedLabel;
 status(result.simulated ? `Demo comment simulated for ${key}.` : `Comment posted to ${key}.`, "success");
      } catch (error) {
        postButton.disabled = false;
        cancelButton.disabled = false;
        reviewButton.disabled = false;
        status(error.message, "error");
      }
    });
    actions.append(copyButton, reviewButton);
    card.append(heading, area, actions, approval);
    comments.append(card);
  }
}

$("generate").addEventListener("click", async () => {
  const button = $("generate");
  button.disabled = true;
  status("Collecting activity and preparing one JSON report…");
  try {
    const data = await api("/api/report", {
      date: $("day").value,
      model: selectedModel(),
    });
    render(data);
    status(`Ready to review ${data.report.items.length} work ${data.report.items.length === 1 ? "item" : "items"}.`, "success");
  } catch (error) {
    status(error.message, "error");
  } finally {
    button.disabled = false;
  }
});

$("loadModels").addEventListener("click", async () => {
  const button = $("loadModels");
  button.disabled = true;
  try {
    const data = await api("/api/models");
    availableModels = data.models;
    const selected = selectedModel() || configuredModel;
    populateModels(availableModels, selected);
    status(`${data.models.length} models available.`, "success");
  } catch (error) {
    status(error.message, "error");
  } finally {
    button.disabled = false;
  }
});

$("copy").addEventListener("click", async () => {
 if (demoMode) {
 status("Demo mode: clipboard copy simulated.", "success");
 return;
 }
  try {
    await navigator.clipboard.writeText($("daily").value);
    status("Daily update copied.", "success");
  } catch (error) {
    status(`Clipboard access failed: ${error.message}`, "error");
  }
});

$("daily").addEventListener("input", () => {
  dailyFormats[dailyFormat] = $("daily").value;
  dailyEdited[dailyFormat] = true;
});

$("dailyFormat").addEventListener("change", () => {
  const nextFormat = $("dailyFormat").value;
  if (nextFormat === dailyFormat) return;
  dailyFormats[dailyFormat] = $("daily").value;
  if (dailyEdited[dailyFormat]) {
    dailyFormats[nextFormat] = convertDailyText($("daily").value, dailyFormat, nextFormat);
    dailyEdited[nextFormat] = true;
  }
  dailyFormat = nextFormat;
  $("daily").value = dailyFormats[dailyFormat];
});

async function discoverRepos() {
  const data = await api("/api/repos");
  const view = $("reposList");
  view.replaceChildren();
  view.append(element("p", `Found ${data.repos.length} ${data.repos.length === 1 ? "repository" : "repositories"} under ${data.roots.length} configured ${data.roots.length === 1 ? "folder" : "folders"}.`));
  for (const warning of data.warnings) view.append(element("p", warning, "warning"));
  if (data.repos.length) {
    const list = document.createElement("ul");
    for (const repo of data.repos) list.append(element("li", repo, "repo-path"));
    view.append(list);
  }
  return data.repos.length;
}

$("discoverRepos").addEventListener("click", async () => {
  const button = $("discoverRepos");
  button.disabled = true;
  try {
    const count = await discoverRepos();
    status(`Found ${count} ${count === 1 ? "repository" : "repositories"}.`, "success");
  } catch (error) {
    status(error.message, "error");
  } finally {
    button.disabled = false;
  }
});

$("saveConfig").addEventListener("click", async () => {
  const button = $("saveConfig");
  button.disabled = true;
  try {
    if (settingsMode === "form") validateFormTimezone(true);
    const previousConfiguredModel = configuredModel;
    const selectedBeforeSave = selectedModel();
    const payload = settingsMode === "form" ? { values: readSettingsForm() } : { text: $("configText").value };
    const data = await api("/api/settings", payload);
    $("configText").value = data.text;
    populateSettingsForm(data.values);
    $("destination").textContent = `AI destination: ${data.ai_endpoint} · summaries use selected excerpts`;
    configuredModel = data.ai_model;
    const selectedAfterSave = selectedBeforeSave === previousConfiguredModel ? data.ai_model : selectedBeforeSave;
    populateModels(availableModels, selectedAfterSave);
    setSettingsFeedback("Settings saved to the configuration file.", "success");
    status("Settings saved.", "success");
    try {
      await discoverRepos();
    } catch (error) {
      setSettingsFeedback(`Settings saved, but repository discovery failed: ${error.message}`, "error");
    }
  } catch (error) {
    if (/timezone|time zone/i.test(error.message)) $("settingTimezone").setAttribute("aria-invalid", "true");
    setSettingsFeedback(error.message, "error");
    status(error.message, "error");
  } finally {
    button.disabled = false;
  }
});

async function start() {
  try {
    const data = await api("/api/settings");
    $("configText").value = data.text;
    $("configPath").textContent = data.path;
    populateSettingsForm(data.values);
    let preferredMode = "form";
    try { preferredMode = localStorage.getItem("recap-settings-mode") === "file" ? "file" : "form"; } catch {}
    showSettingsMode(preferredMode);
    $("destination").textContent = `AI destination: ${data.ai_endpoint} · summaries use selected excerpts`;
    configuredModel = data.ai_model;
    populateModels([], configuredModel);
    $("day").value = data.day;
    if (demoMode) status("Demo mode · synthetic data and simulated actions.", "success");
  } catch (error) {
    setSettingsFeedback(error.message, "error");
    status(error.message, "error");
  }
}

start();
