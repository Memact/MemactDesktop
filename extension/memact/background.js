const BRIDGE_URL = "http://127.0.0.1:38453/session";
const EXTENSION_VERSION = chrome.runtime.getManifest().version;
const SNIPPET_MAX_LEN = 280;
const FULL_TEXT_MAX_LEN = 8000;

function detectBrowserKey() {
  const userAgent = navigator.userAgent || "";
  if (userAgent.includes("Edg/")) {
    return "edge";
  }
  if (userAgent.includes("OPR/")) {
    return "opera";
  }
  if (userAgent.includes("Vivaldi/")) {
    return "vivaldi";
  }
  if (userAgent.includes("Brave/")) {
    return "brave";
  }
  return "chrome";
}

async function snapshotFocusedWindow() {
  try {
    const currentWindow = await chrome.windows.getLastFocused({ populate: true });
    if (!currentWindow || !Array.isArray(currentWindow.tabs)) {
      return;
    }

    const browser = detectBrowserKey();
    const tabs = currentWindow.tabs
      .filter((tab) => tab && tab.url)
      .map((tab) => ({
        id: tab.id,
        title: tab.title || "",
        url: tab.url || "",
        active: Boolean(tab.active)
      }));
    const activeTab = currentWindow.tabs.find((tab) => tab && tab.active);
    const activeContext = activeTab ? await captureActiveTabContext(activeTab) : null;

    await fetch(BRIDGE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        browser,
        extensionVersion: EXTENSION_VERSION,
        windowId: currentWindow.id,
        tabs,
        activeContext
      })
    });
  } catch (error) {
    // Keep the extension silent when Memact is not running.
  }
}

let snapshotTimer = null;

function normalizeText(value, maxLen) {
  const text = String(value || "")
    .replace(/\s+/g, " ")
    .trim();
  if (!text) return "";
  if (!maxLen) return text;
  return text.length > maxLen ? `${text.slice(0, maxLen - 3)}...` : text;
}

function truncateText(value, maxLen) {
  const text = String(value || "");
  if (!text || !maxLen) {
    return text;
  }
  return text.length > maxLen ? text.slice(0, maxLen) : text;
}

async function injectReadability(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["Readability.js"]
    });
    return true;
  } catch (error) {
    return false;
  }
}

async function captureActiveTabContext(tab) {
  if (!tab || !tab.id || !tab.url) {
    return null;
  }
  if (!/^https?:|^file:/i.test(tab.url)) {
    return null;
  }
  try {
    const readabilityReady = await injectReadability(tab.id);
    const [injected] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      args: [SNIPPET_MAX_LEN, FULL_TEXT_MAX_LEN, readabilityReady],
      func: (snippetMaxLen, fullTextMaxLen, canUseReadability) => {
        if (!window.__memactCaptureInstalled) {
          window.__memactCaptureInstalled = true;
          window.__memactLastInputAt = 0;
          window.__memactLastScrollAt = 0;
          window.addEventListener(
            "input",
            () => {
              window.__memactLastInputAt = Date.now();
            },
            true
          );
          window.addEventListener(
            "scroll",
            () => {
              window.__memactLastScrollAt = Date.now();
            },
            true
          );
        }
        const normalizeVisibleText = (value) =>
          String(value || "")
            .replace(/\s+/g, " ")
            .trim();
        const pickContentText = () => {
          const selectorGroups = [
            "article",
            "main",
            "[role='main']",
            "[role='article']",
            "[role='document']",
            "[role='feed']",
            "[role='log']",
            "[aria-live='polite']",
            "[aria-live='assertive']",
            "[data-testid*='conversation']",
            "[data-testid*='message']",
            "[data-testid*='messages']",
            "[class*='conversation']",
            "[class*='message-list']",
            "[class*='messageList']",
            "[class*='messages']",
            "[class*='chat']",
            "[class*='thread']"
          ];
          const seen = new Set();
          const candidates = [];
          for (const selector of selectorGroups) {
            const nodes = document.querySelectorAll(selector);
            for (const node of nodes) {
              const text = normalizeVisibleText(node?.innerText || "");
              if (!text) {
                continue;
              }
              const key = text.slice(0, 600);
              if (seen.has(key)) {
                continue;
              }
              seen.add(key);
              candidates.push(text);
            }
          }
          candidates.sort((left, right) => right.length - left.length);
          const bodyText = normalizeVisibleText(document.body?.innerText || "");
          if (candidates.length && candidates[0].length >= 120) {
            return candidates[0];
          }
          if (bodyText) {
            return bodyText;
          }
          return candidates[0] || "";
        };
        const readMeta = (key, attr = "name") => {
          const selector = `meta[${attr}="${key}"]`;
          const el = document.querySelector(selector);
          return el ? el.getAttribute("content") || "" : "";
        };
        const ogTitle = readMeta("og:title", "property");
        const ogDescription = readMeta("og:description", "property");
        const description = readMeta("description") || ogDescription;
        const pageTitle = document.title || ogTitle || "";
        const h1 = document.querySelector("h1")?.innerText || "";
        const selection = window.getSelection()?.toString() || "";
        const pageContent = pickContentText();
        const snippet = pageContent.slice(0, snippetMaxLen);
        let fullText = "";
        if (canUseReadability && typeof Readability === "function") {
          try {
            const clonedDocument = document.cloneNode(true);
            const articleData = new Readability(clonedDocument).parse();
            const articleText = normalizeVisibleText(articleData?.textContent || "");
            if (articleText) {
              fullText = articleText.slice(0, fullTextMaxLen);
            }
          } catch (error) {
            fullText = "";
          }
        }
        if (!fullText && pageContent) {
          fullText = pageContent.slice(0, fullTextMaxLen);
        }
        const now = Date.now();
        const activeEl = document.activeElement;
        const activeTag = activeEl?.tagName || "";
        const activeType = activeEl?.type || "";
        const isEditable = Boolean(activeEl?.isContentEditable);
        const typingActive =
          window.__memactLastInputAt &&
          now - window.__memactLastInputAt < 5000 &&
          (activeTag === "INPUT" || activeTag === "TEXTAREA" || isEditable);
        const scrollingActive =
          window.__memactLastScrollAt && now - window.__memactLastScrollAt < 4000;
        return {
          pageTitle,
          description,
          h1,
          selection,
          snippet,
          ...(fullText ? { fullText } : {}),
          activeTag,
          activeType,
          typingActive,
          scrollingActive
        };
      }
    });
    const result = injected && injected.result ? injected.result : null;
    if (!result) {
      return null;
    }
    return {
      pageTitle: normalizeText(result.pageTitle, 140),
      description: normalizeText(result.description, 200),
      h1: normalizeText(result.h1, 120),
      selection: normalizeText(result.selection, 200),
      snippet: normalizeText(result.snippet, SNIPPET_MAX_LEN),
      ...(result.fullText
        ? { fullText: truncateText(normalizeText(result.fullText), FULL_TEXT_MAX_LEN) }
        : {}),
      activeTag: normalizeText(result.activeTag, 40),
      activeType: normalizeText(result.activeType, 40),
      typingActive: Boolean(result.typingActive),
      scrollingActive: Boolean(result.scrollingActive)
    };
  } catch (error) {
    return null;
  }
}

function queueSnapshot() {
  clearTimeout(snapshotTimer);
  snapshotTimer = setTimeout(() => {
    snapshotFocusedWindow();
  }, 250);
}

chrome.runtime.onInstalled.addListener(queueSnapshot);
chrome.runtime.onStartup.addListener(queueSnapshot);
chrome.tabs.onActivated.addListener(queueSnapshot);
chrome.tabs.onUpdated.addListener(queueSnapshot);
chrome.tabs.onCreated.addListener(queueSnapshot);
chrome.tabs.onRemoved.addListener(queueSnapshot);
chrome.windows.onFocusChanged.addListener(queueSnapshot);
