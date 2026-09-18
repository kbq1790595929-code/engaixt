using BepInEx;
using HarmonyLib;
using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Reflection;
using System.Text;
using System.Threading;
using UnityEngine;
using UnityEngine.UI;

[BepInPlugin("game-translator.ugui-overlay-guard", "Game Translator UGUI Overlay Guard", "1.4.4")]
public sealed class UGUITextOverlayGuard : BaseUnityPlugin
{
    private const string RedirectMarker = "\u180e";
    private const string TranslateUrl = "http://127.0.0.1:5120/translate";
    private static readonly bool EnableHarmonyHook = false;
    private const float StableTextDelaySeconds = 0.65f;
    private const float RequestIntervalSeconds = 0.8f;
    private const float RetryDelaySeconds = 12f;
    private static readonly Dictionary<Text, State> States = new Dictionary<Text, State>();
    private static readonly HashSet<Text> OverlayTexts = new HashSet<Text>();
    private static readonly Dictionary<string, string> Cache = new Dictionary<string, string>();
    private static readonly HashSet<string> Pending = new HashSet<string>();
    private static readonly Queue<string> RequestQueue = new Queue<string>();
    private static readonly Queue<TranslationResult> CompletedRequests = new Queue<TranslationResult>();
    private static readonly object CompletedRequestsLock = new object();
    private static readonly Dictionary<string, float> RetryAfter = new Dictionary<string, float>();
    private static readonly HashSet<string> Queued = new HashSet<string>();
    private static readonly HashSet<string> LoggedCandidates = new HashSet<string>();
    private static readonly HashSet<string> LoggedFailures = new HashSet<string>();
    private static int _queuedThisSession;
    private static int _translatedThisSession;
    private static UGUITextOverlayGuard _instance;
    private static readonly FieldInfo TextField =
        typeof(Text).GetField("m_Text", BindingFlags.Instance | BindingFlags.NonPublic);
    private static bool _internalSet;
    private static float _nextScanAt;
    private static float _nextCacheLoadAt;
    private static float _nextRequestAt;
    private static bool _requestInFlight;

    private void Awake()
    {
        _instance = this;
        LoadCache();
        StartCoroutine(SyncRoutine());
        StartCoroutine(RequestWorkerRoutine());
        if (EnableHarmonyHook)
        {
            try
            {
                Harmony.CreateAndPatchAll(typeof(TextSetPatch), "game-translator.ugui-overlay-guard");
            }
            catch (Exception e)
            {
                Logger.LogWarning("UGUI Overlay Guard Harmony hook disabled: " + e.GetType().Name + " " + e.Message);
            }
        }
        Logger.LogInfo("UGUI Overlay Guard loaded (polling overlay + debounced live proxy mode)");
    }

    private static IEnumerator SyncRoutine()
    {
        var wait = new WaitForSecondsRealtime(0.05f);
        while (true)
        {
            var now = Now();
            if (now >= _nextCacheLoadAt)
            {
                LoadCache();
                _nextCacheLoadAt = now + 5f;
            }
            ProcessCompletedRequests();
            if (now >= _nextScanAt)
            {
                ScanVisibleTexts();
                _nextScanAt = now + 0.25f;
            }

            var dead = new List<Text>();
            foreach (var pair in States)
            {
                var source = pair.Key;
                var state = pair.Value;
                if (source == null)
                {
                    dead.Add(source);
                    continue;
                }
                if (state.HasOverlay && !string.IsNullOrEmpty(state.Translated) && !state.DisplayApplied)
                {
                    SyncOverlay(source, state);
                    HideSource(source);
                    state.DisplayApplied = true;
                }
                else if (state.HasOverlay && !string.IsNullOrEmpty(state.Translated) && state.DisplayApplied)
                {
                    bool sourceActive = source.gameObject.activeInHierarchy && source.enabled;
                    if (state.Overlay != null && state.Overlay.gameObject.activeSelf != sourceActive)
                    {
                        state.Overlay.gameObject.SetActive(sourceActive);
                    }
                }
            }
            foreach (var item in dead)
            {
                States.Remove(item);
            }
            yield return wait;
        }
    }

    private sealed class State
    {
        public string Original = "";
        public string Translated = "";
        public string LastSeen = "";
        public float LastChangedAt;
        public Text Overlay;
        public bool HasOverlay;
        public bool DisplayApplied;
    }

    private sealed class TranslationResult
    {
        public string Original = "";
        public string Translated = "";
        public string Error = "";
        public bool Success;
    }

    [HarmonyPatch]
    private static class TextSetPatch
    {
        private static MethodBase TargetMethod()
        {
            return AccessTools.PropertySetter(typeof(Text), "text");
        }

        [HarmonyPriority(Priority.First)]
        private static bool Prefix(Text __instance, string value)
        {
            if (_internalSet || __instance == null || OverlayTexts.Contains(__instance))
            {
                return true;
            }

            if (value != null && value.StartsWith(RedirectMarker, StringComparison.Ordinal))
            {
                var state = GetState(__instance);
                if (string.IsNullOrEmpty(state.Original))
                {
                    state.Original = StripMarker(CurrentText(__instance));
                }

                state.Translated = value.Substring(RedirectMarker.Length);
                EnsureOverlay(__instance, state);
                SyncOverlay(__instance, state);
                HideSource(__instance);
                SetRawText(__instance, state.Original ?? "");
                state.DisplayApplied = true;
                return false;
            }

            var next = value ?? "";
            var existing = GetState(__instance);
            UpdateObservedText(existing, next);
            return true;
        }

        private static void Postfix(Text __instance)
        {
            if (_internalSet || __instance == null || OverlayTexts.Contains(__instance))
            {
                return;
            }
            ApplyCachedOverlay(__instance);
        }
    }

    private static void ScanVisibleTexts()
    {
        try
        {
            var texts = Resources.FindObjectsOfTypeAll<Text>();
            foreach (var text in texts)
            {
                if (text == null || OverlayTexts.Contains(text))
                {
                    continue;
                }
                if (!text.gameObject.activeInHierarchy || !text.enabled)
                {
                    continue;
                }
                ApplyCachedOverlay(text);
            }
        }
        catch
        {
        }
    }

    private static void ApplyCachedOverlay(Text source)
    {
        var raw = StripMarker(CurrentText(source));
        var state = GetState(source);
        UpdateObservedText(state, raw);

        string translated;
        if (!TryGetTranslation(raw, out translated))
        {
            state.Translated = "";
            HideOverlay(source, state);
            RestoreSource(source);
            QueueStableTranslation(state, raw);
            return;
        }

        state.Translated = translated;
        EnsureOverlay(source, state);
        SyncOverlay(source, state);
        HideSource(source);
        state.DisplayApplied = true;
    }

    private static bool TryGetTranslation(string original, out string translated)
    {
        translated = "";
        if (string.IsNullOrEmpty(original))
        {
            return false;
        }

        if (Cache.TryGetValue(original, out translated))
        {
            translated = StripMarker(translated);
            return !string.IsNullOrEmpty(translated) && translated != original;
        }
        var normalized = original.Replace("\r", "\\r").Replace("\n", "\\n");
        if (Cache.TryGetValue(normalized, out translated))
        {
            translated = StripMarker(translated);
            return !string.IsNullOrEmpty(translated) && translated != original;
        }
        return false;
    }

    private static void QueueStableTranslation(State state, string original)
    {
        if (state == null || string.IsNullOrEmpty(original))
        {
            return;
        }
        if (Now() - state.LastChangedAt < StableTextDelaySeconds)
        {
            return;
        }
        QueueTranslation(original);
    }

    private static void QueueTranslation(string original)
    {
        if (_instance == null || string.IsNullOrEmpty(original) || original.Length > 1000)
        {
            return;
        }
        if (!LooksTranslatable(original))
        {
            return;
        }
        float retryAt;
        if (RetryAfter.TryGetValue(original, out retryAt) && Now() < retryAt)
        {
            return;
        }
        if (Pending.Contains(original) || Queued.Contains(original) || Cache.ContainsKey(original) || LooksAlreadyTranslated(original))
        {
            return;
        }
        Queued.Add(original);
        RequestQueue.Enqueue(original);
        _queuedThisSession++;
        if (LoggedCandidates.Add(original) || _queuedThisSession <= 20)
        {
            _instance.Logger.LogInfo("UGUI live translate queued: " + Short(original));
        }
    }

    private static IEnumerator RequestWorkerRoutine()
    {
        var wait = new WaitForSecondsRealtime(0.1f);
        while (true)
        {
            if (_instance == null || _requestInFlight || RequestQueue.Count == 0)
            {
                yield return wait;
                continue;
            }

            if (Now() < _nextRequestAt)
            {
                yield return wait;
                continue;
            }

            var original = RequestQueue.Dequeue();
            Queued.Remove(original);
            if (string.IsNullOrEmpty(original) || Cache.ContainsKey(original) || Pending.Contains(original))
            {
                continue;
            }
            Pending.Add(original);
            if (_translatedThisSession < 50)
            {
                _instance.Logger.LogInfo("UGUI live translate sending: " + Short(original));
            }
            _requestInFlight = true;
            BeginTranslateRequest(original);
        }
    }

    private static void BeginTranslateRequest(string original)
    {
        ThreadPool.QueueUserWorkItem(_ =>
        {
            var result = new TranslationResult { Original = original };
            try
            {
                var json = "{\"text\":\"" + JsonEscape(original) + "\",\"from\":\"en\",\"to\":\"zh\"}";
                var body = Encoding.UTF8.GetBytes(json);
                var request = (HttpWebRequest)WebRequest.Create(TranslateUrl);
                request.Method = "POST";
                request.ContentType = "application/json; charset=utf-8";
                request.Accept = "text/plain, application/json, */*";
                request.Timeout = 20000;
                request.ReadWriteTimeout = 20000;
                request.Proxy = null;
                request.ContentLength = body.Length;

                using (var stream = request.GetRequestStream())
                {
                    stream.Write(body, 0, body.Length);
                }

                using (var response = (HttpWebResponse)request.GetResponse())
                using (var stream = response.GetResponseStream())
                using (var reader = new StreamReader(stream, Encoding.UTF8))
                {
                    result.Translated = StripMarker(reader.ReadToEnd() ?? "").Trim();
                    result.Success = response.StatusCode >= HttpStatusCode.OK &&
                                     response.StatusCode < HttpStatusCode.MultipleChoices;
                }
            }
            catch (Exception e)
            {
                result.Error = e.GetType().Name + " " + e.Message;
                var web = e as WebException;
                if (web != null && web.Response != null)
                {
                    try
                    {
                        using (var response = web.Response)
                        using (var stream = response.GetResponseStream())
                        using (var reader = new StreamReader(stream, Encoding.UTF8))
                        {
                            var body = reader.ReadToEnd();
                            if (!string.IsNullOrEmpty(body))
                            {
                                result.Error += " | " + Short(body);
                            }
                        }
                    }
                    catch
                    {
                    }
                }
            }

            lock (CompletedRequestsLock)
            {
                CompletedRequests.Enqueue(result);
            }
        });
    }

    private static void ProcessCompletedRequests()
    {
        while (true)
        {
            TranslationResult result = null;
            lock (CompletedRequestsLock)
            {
                if (CompletedRequests.Count > 0)
                {
                    result = CompletedRequests.Dequeue();
                }
            }
            if (result == null)
            {
                break;
            }

            Pending.Remove(result.Original);
            _requestInFlight = false;
            _nextRequestAt = Now() + RequestIntervalSeconds;

            if (result.Success)
            {
                var translated = StripMarker(result.Translated).Trim();
                if (!string.IsNullOrEmpty(translated) && translated != result.Original)
                {
                    Cache[result.Original] = translated;
                    RetryAfter.Remove(result.Original);
                    AppendCacheLine(result.Original, translated);
                    _translatedThisSession++;
                    if (_translatedThisSession <= 50)
                    {
                        _instance.Logger.LogInfo("UGUI live translate OK: " + Short(result.Original) + " => " + Short(translated));
                    }
                }
                else if (LoggedFailures.Add(result.Original))
                {
                    _instance.Logger.LogWarning("UGUI live translate empty/same: " + Short(result.Original));
                    RetryAfter[result.Original] = Now() + RetryDelaySeconds;
                }
                continue;
            }

            RetryAfter[result.Original] = Now() + RetryDelaySeconds;
            if (LoggedFailures.Add(result.Original))
            {
                _instance.Logger.LogWarning("UGUI live translate failed: " + result.Error + " | " + Short(result.Original));
            }
        }
    }

    private static void UpdateObservedText(State state, string raw)
    {
        raw = raw ?? "";
        state.Original = raw;
        if (state.LastSeen != raw)
        {
            state.LastSeen = raw;
            state.LastChangedAt = Now();
            state.DisplayApplied = false;
        }
    }

    private static float Now()
    {
        return Time.realtimeSinceStartup;
    }

    private static void AppendCacheLine(string original, string translated)
    {
        try
        {
            var file = AutoGeneratedCachePath();
            Directory.CreateDirectory(Path.GetDirectoryName(file));
            File.AppendAllText(file, EscapeKey(original) + "=" + RedirectMarker + translated + Environment.NewLine,
                new UTF8Encoding(true));
        }
        catch
        {
        }
    }

    private static string AutoGeneratedCachePath()
    {
        return CombinePath(Paths.GameRootPath, "BepInEx", "Translation", "zh", "Text",
            "_AutoGeneratedTranslations.txt");
    }

    private static void LoadCache()
    {
        try
        {
            var root = Paths.GameRootPath;
            var candidates = new[]
            {
                AutoGeneratedCachePath(),
                CombinePath(root, "BepInEx", "Translation", "zh", "Translation_zh.txt")
            };
            foreach (var file in candidates)
            {
                if (!File.Exists(file))
                {
                    continue;
                }
                foreach (var line in File.ReadAllLines(file))
                {
                    ParseCacheLine(line);
                }
            }
        }
        catch
        {
        }
    }

    private static string CombinePath(params string[] parts)
    {
        if (parts == null || parts.Length == 0)
        {
            return "";
        }
        var path = parts[0] ?? "";
        for (var i = 1; i < parts.Length; i++)
        {
            path = Path.Combine(path, parts[i] ?? "");
        }
        return path;
    }

    private static void ParseCacheLine(string line)
    {
        if (string.IsNullOrEmpty(line) || line.StartsWith("#", StringComparison.Ordinal))
        {
            return;
        }
        var idx = FindSeparator(line);
        if (idx <= 0)
        {
            return;
        }
        var key = UnescapeKey(line.Substring(0, idx).Trim());
        var value = StripMarker(line.Substring(idx + 1).Trim());
        if (!string.IsNullOrEmpty(key) && !string.IsNullOrEmpty(value) && key != value)
        {
            Cache[key] = value;
        }
    }

    private static int FindSeparator(string line)
    {
        var escaped = false;
        for (var i = 0; i < line.Length; i++)
        {
            var ch = line[i];
            if (escaped)
            {
                escaped = false;
                continue;
            }
            if (ch == '\\')
            {
                escaped = true;
                continue;
            }
            if (ch == '=')
            {
                return i;
            }
        }
        return -1;
    }

    private static string UnescapeKey(string key)
    {
        return key.Replace("\\=", "=").Replace("\\#", "#").Replace("\\n", "\n").Replace("\\r", "\r");
    }

    private static string EscapeKey(string key)
    {
        return (key ?? "").Replace("\\", "\\\\").Replace("\r", "\\r").Replace("\n", "\\n")
            .Replace("=", "\\=").Replace("#", "\\#");
    }

    private static string JsonEscape(string value)
    {
        return (value ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"")
            .Replace("\r", "\\r").Replace("\n", "\\n").Replace("\t", "\\t");
    }

    private static bool LooksAlreadyTranslated(string text)
    {
        var cjk = 0;
        foreach (var ch in text)
        {
            if ((ch >= '\u4e00' && ch <= '\u9fff') || (ch >= '\u3400' && ch <= '\u4dbf'))
            {
                cjk++;
            }
        }
        return text.Length > 0 && cjk * 5 >= text.Length;
    }

    private static bool LooksTranslatable(string text)
    {
        if (string.IsNullOrEmpty(text))
        {
            return false;
        }
        var trimmed = text.Trim();
        if (trimmed.Length < 2 || trimmed.Length > 1000)
        {
            return false;
        }
        var letters = 0;
        var spaces = 0;
        foreach (var ch in trimmed)
        {
            if ((ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z'))
            {
                letters++;
            }
            else if (char.IsWhiteSpace(ch))
            {
                spaces++;
            }
        }
        if (letters < 2)
        {
            return false;
        }
        if (trimmed.IndexOf("/") >= 0 || trimmed.IndexOf("\\") >= 0 ||
            trimmed.IndexOf(".png", StringComparison.OrdinalIgnoreCase) >= 0)
        {
            return false;
        }
        if (trimmed.Length < 8 && spaces == 0)
        {
            return false;
        }
        return spaces > 0 || trimmed.Length <= 40;
    }

    private static string Short(string text)
    {
        if (string.IsNullOrEmpty(text))
        {
            return "";
        }
        text = text.Replace("\r", " ").Replace("\n", " ");
        return text.Length <= 80 ? text : text.Substring(0, 80) + "...";
    }

    private static State GetState(Text source)
    {
        State state;
        if (!States.TryGetValue(source, out state))
        {
            state = new State();
            States[source] = state;
        }
        return state;
    }

    private static string CurrentText(Text source)
    {
        try
        {
            if (TextField != null)
            {
                return TextField.GetValue(source) as string ?? "";
            }
        }
        catch
        {
        }
        return source.text ?? "";
    }

    private static string StripMarker(string value)
    {
        if (value != null && value.StartsWith(RedirectMarker, StringComparison.Ordinal))
        {
            return value.Substring(RedirectMarker.Length);
        }
        return value ?? "";
    }

    private static void SetRawText(Text source, string value)
    {
        try
        {
            if (TextField != null)
            {
                TextField.SetValue(source, value ?? "");
                source.SetVerticesDirty();
                source.SetLayoutDirty();
                return;
            }
        }
        catch
        {
        }

        _internalSet = true;
        try
        {
            source.text = value ?? "";
        }
        finally
        {
            _internalSet = false;
        }
    }

    private static void EnsureOverlay(Text source, State state)
    {
        if (state.Overlay != null)
        {
            return;
        }

        var go = new GameObject("GT_UGUI_TranslationOverlay");
        go.transform.SetParent(source.transform, false);
        var rt = go.AddComponent<RectTransform>();
        rt.anchorMin = Vector2.zero;
        rt.anchorMax = Vector2.one;
        rt.offsetMin = Vector2.zero;
        rt.offsetMax = Vector2.zero;
        rt.localRotation = Quaternion.identity;
        rt.localScale = Vector3.one;

        var overlay = go.AddComponent<Text>();
        overlay.raycastTarget = false;
        OverlayTexts.Add(overlay);
        overlay.transform.SetAsLastSibling();
        state.Overlay = overlay;
        state.HasOverlay = true;
    }

    private static void SyncOverlay(Text source, State state)
    {
        var overlay = state.Overlay;
        if (overlay == null)
        {
            state.HasOverlay = false;
            return;
        }

        overlay.gameObject.SetActive(source.gameObject.activeInHierarchy && source.enabled);
        SetRawText(overlay, state.Translated ?? "");
        overlay.font = source.font;
        overlay.fontSize = source.fontSize;
        overlay.fontStyle = source.fontStyle;
        overlay.alignment = source.alignment;
        overlay.alignByGeometry = source.alignByGeometry;
        overlay.resizeTextForBestFit = source.resizeTextForBestFit;
        overlay.resizeTextMinSize = source.resizeTextMinSize;
        overlay.resizeTextMaxSize = source.resizeTextMaxSize;
        overlay.horizontalOverflow = source.horizontalOverflow;
        overlay.verticalOverflow = source.verticalOverflow;
        overlay.lineSpacing = source.lineSpacing;
        overlay.supportRichText = source.supportRichText;
        overlay.color = source.color;
        overlay.material = source.material;

        var rt = overlay.rectTransform;
        rt.anchorMin = Vector2.zero;
        rt.anchorMax = Vector2.one;
        rt.offsetMin = Vector2.zero;
        rt.offsetMax = Vector2.zero;
        rt.localRotation = Quaternion.identity;
        rt.localScale = Vector3.one;
    }

    private static void HideOverlay(Text source, State state)
    {
        if (state.Overlay != null)
        {
            state.Overlay.gameObject.SetActive(false);
        }
        state.HasOverlay = false;
        state.DisplayApplied = false;
    }

    private static void HideSource(Text source)
    {
        if (source.canvasRenderer != null)
        {
            source.canvasRenderer.SetAlpha(0f);
        }
    }

    private static void RestoreSource(Text source)
    {
        if (source.canvasRenderer != null)
        {
            source.canvasRenderer.SetAlpha(1f);
        }
    }
}
