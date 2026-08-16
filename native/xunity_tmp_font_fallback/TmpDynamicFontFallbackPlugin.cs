using BepInEx;
using BepInEx.Logging;
using BepInEx.Unity.IL2CPP;
using Il2CppInterop.Runtime.Injection;
using Il2CppInterop.Runtime.InteropTypes;
using TMPro;
using UnityEngine;
using UnityObject = UnityEngine.Object;

namespace EngAixt.XUnityTmpFontFallback;

[BepInPlugin(PluginGuid, PluginName, PluginVersion)]
public sealed class TmpDynamicFontFallbackPlugin : BasePlugin
{
    public const string PluginGuid = "engaixt.xunity.tmp-dynamic-font-fallback";
    public const string PluginName = "EngAixt TMP CJK Fallback";
    public const string PluginVersion = "1.1.0";

    private const string FontBundleName = "NotoSansSC_sdf32_optimized_12k_lz4_2020";
    private static ManualLogSource _log;

    public override void Load()
    {
        var bundlePath = System.IO.Path.Combine(Paths.GameRootPath, FontBundleName);
        if (!System.IO.File.Exists(bundlePath))
        {
            Log.LogInfo($"TMP CJK fallback bundle not present: {FontBundleName}");
            return;
        }

        _log = Log;
        ClassInjector.RegisterTypeInIl2Cpp<TmpFontFallbackLoader>();
        var host = new GameObject("EngAixt.TmpFontFallbackLoader");
        UnityObject.DontDestroyOnLoad(host);
        host.AddComponent<TmpFontFallbackLoader>().Begin(bundlePath);
        Log.LogInfo($"TMP CJK fallback loading asynchronously: {FontBundleName}");
    }

    internal static void Report(string message, bool isError = false)
    {
        if (isError)
        {
            _log?.LogError(message);
        }
        else
        {
            _log?.LogInfo(message);
        }
    }
}

public sealed class TmpFontFallbackLoader : MonoBehaviour
{
    private AssetBundleCreateRequest _bundleRequest;
    private AssetBundleRequest _fontRequest;
    private AssetBundle _bundle;
    private bool _completed;
    private string _bundlePath;

    public TmpFontFallbackLoader(System.IntPtr pointer)
        : base(pointer)
    {
    }

    public void Begin(string bundlePath)
    {
        _bundlePath = bundlePath;
    }

    public void Update()
    {
        if (_completed || string.IsNullOrEmpty(_bundlePath))
        {
            return;
        }

        try
        {
            if (_bundleRequest == null)
            {
                _bundleRequest = AssetBundle.LoadFromFileAsync(_bundlePath);
                if (_bundleRequest == null)
                {
                    Complete("TMP CJK fallback failed: LoadFromFileAsync returned null", true);
                }
                return;
            }

            if (_bundle == null)
            {
                if (!_bundleRequest.isDone)
                {
                    return;
                }

                _bundle = _bundleRequest.assetBundle;
                if (_bundle == null)
                {
                    Complete("TMP CJK fallback failed: asset bundle could not be loaded", true);
                    return;
                }

                _fontRequest = _bundle.LoadAllAssetsAsync<TMP_FontAsset>();
                return;
            }

            if (_fontRequest == null || !_fontRequest.isDone)
            {
                return;
            }

            foreach (var asset in _fontRequest.allAssets)
            {
                var font = asset.TryCast<TMP_FontAsset>();
                if (font == null)
                {
                    continue;
                }

                var fallbacks = TMP_Settings.fallbackFontAssets;
                if (fallbacks == null)
                {
                    fallbacks = new Il2CppSystem.Collections.Generic.List<TMP_FontAsset>();
                    TMP_Settings.fallbackFontAssets = fallbacks;
                }

                fallbacks.Add(font);
                UnityObject.DontDestroyOnLoad(_bundle);
                UnityObject.DontDestroyOnLoad(font);
                Complete($"TMP CJK fallback loaded: {font.name}");
                return;
            }

            Complete("TMP CJK fallback failed: bundle contains no TMP_FontAsset", true);
        }
        catch (System.Exception exception)
        {
            Complete($"TMP CJK fallback failed: {exception}", true);
        }
    }

    private void Complete(string message, bool isError = false)
    {
        _completed = true;
        TmpDynamicFontFallbackPlugin.Report(message, isError);
    }
}
