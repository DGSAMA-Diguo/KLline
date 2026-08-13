package com.gupiap.kline;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.webkit.JsResult;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Collections;

@SuppressWarnings("deprecation")
public final class MainActivity extends Activity {
    private static final String LOCAL_HOST = "appassets.androidplatform.net";
    private static final String LIVE_HOST = "push2delay.eastmoney.com";
    private static final String LOCAL_PATH = "/assets/KLineMobile.html";
    private static final String HOME_URL = "https://" + LOCAL_HOST + LOCAL_PATH;

    private WebView webView;
    private UpdateManager updateManager;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // 启动时清理上次运行遗留的网页缓存、更新包、下载残片和日志。
        cleanApplicationGarbage();

        webView = new WebView(this);
        webView.setBackgroundColor(Color.rgb(244, 247, 251));
        WebView.setWebContentsDebuggingEnabled(false);
        webView.clearCache(true);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setCacheMode(WebSettings.LOAD_NO_CACHE);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setMediaPlaybackRequiresUserGesture(true);
        settings.setSupportMultipleWindows(false);

        // 本地页面使用安全虚拟地址，实时接口仍按浏览器跨域规则访问。
        webView.setWebViewClient(new RestrictedWebViewClient());
        // 支持 JavaScript 的 confirm 弹窗，收藏删除等功能依赖此回调。
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onJsConfirm(
                    WebView view,
                    String url,
                    String message,
                    JsResult result
            ) {
                new AlertDialog.Builder(view.getContext())
                        .setMessage(message)
                        .setPositiveButton(
                                "确定",
                                (DialogInterface dialog, int which) -> result.confirm()
                        )
                        .setNegativeButton(
                                "取消",
                                (DialogInterface dialog, int which) -> result.cancel()
                        )
                        .setOnCancelListener(
                                (DialogInterface dialog) -> result.cancel()
                        )
                        .show();
                return true;
            }
        });
        setContentView(webView);

        if (savedInstanceState == null) {
            webView.loadUrl(HOME_URL);
        } else {
            webView.restoreState(savedInstanceState);
        }

        // 启动后在后台检查码云仓库，不阻塞本地行情界面。
        updateManager = new UpdateManager(this);
        updateManager.checkForUpdates();
    }

    private void cleanApplicationGarbage() {
        deleteChildren(getCacheDir());
        deleteChildren(getExternalCacheDir());
        deleteDirectory(new File(getFilesDir(), "logs"));
    }

    private static void deleteChildren(File directory) {
        if (directory == null || !directory.isDirectory()) {
            return;
        }
        try {
            String rootPath = directory.getCanonicalPath();
            File[] children = directory.listFiles();
            if (children == null) {
                return;
            }
            for (File child : children) {
                deleteRecursively(child, rootPath);
            }
        } catch (IOException ignored) {
            // 缓存目录异常时保持静默，不影响应用启动。
        }
    }

    private static void deleteDirectory(File directory) {
        if (directory == null || !directory.exists()) {
            return;
        }
        try {
            deleteRecursively(directory, directory.getCanonicalPath());
        } catch (IOException ignored) {
            // 日志目录异常时保持静默，不影响应用启动。
        }
    }

    private static void deleteRecursively(File target, String rootPath)
            throws IOException {
        if (target == null || !target.exists()) {
            return;
        }
        String targetPath = target.getCanonicalPath();
        if (!targetPath.equals(rootPath)
                && !targetPath.startsWith(rootPath + File.separator)) {
            return;
        }
        if (target.isDirectory()) {
            File[] children = target.listFiles();
            if (children != null) {
                for (File child : children) {
                    deleteRecursively(child, rootPath);
                }
            }
        }
        // 清理失败时保持静默，不影响离线行情和应用启动。
        target.delete();
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        webView.saveState(outState);
        super.onSaveInstanceState(outState);
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
            return;
        }
        super.onBackPressed();
    }

    @Override
    protected void onActivityResult(
            int requestCode,
            int resultCode,
            Intent data
    ) {
        super.onActivityResult(requestCode, resultCode, data);
        if (updateManager != null) {
            updateManager.onActivityResult(requestCode);
        }
    }

    @Override
    protected void onDestroy() {
        if (updateManager != null) {
            updateManager.destroy();
            updateManager = null;
        }
        if (webView != null) {
            webView.stopLoading();
            webView.setWebViewClient(null);
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }

    private final class RestrictedWebViewClient extends WebViewClient {
        @Override
        public WebResourceResponse shouldInterceptRequest(
                WebView view,
                WebResourceRequest request
        ) {
            Uri uri = request.getUrl();
            if (isLocalPage(uri)) {
                return openLocalPage();
            }
            if (isLiveMarketRequest(uri)) {
                return super.shouldInterceptRequest(view, request);
            }

            // 拒绝页面访问未列入白名单的网络资源。
            return blockedResponse();
        }

        @Override
        public boolean shouldOverrideUrlLoading(
                WebView view,
                WebResourceRequest request
        ) {
            Uri uri = request.getUrl();
            return !isLocalPage(uri);
        }

        private boolean isLocalPage(Uri uri) {
            return "https".equalsIgnoreCase(uri.getScheme())
                    && LOCAL_HOST.equalsIgnoreCase(uri.getHost())
                    && LOCAL_PATH.equals(uri.getPath());
        }

        private boolean isLiveMarketRequest(Uri uri) {
            return "https".equalsIgnoreCase(uri.getScheme())
                    && LIVE_HOST.equalsIgnoreCase(uri.getHost());
        }

        private WebResourceResponse openLocalPage() {
            try {
                InputStream stream = getAssets().open("KLineMobile.html");
                return new WebResourceResponse(
                        "text/html",
                        "UTF-8",
                        200,
                        "OK",
                        Collections.singletonMap("Cache-Control", "no-store"),
                        stream
                );
            } catch (IOException error) {
                return errorResponse("内置页面读取失败");
            }
        }

        private WebResourceResponse blockedResponse() {
            return errorResponse("请求已被应用安全策略阻止");
        }

        private WebResourceResponse errorResponse(String message) {
            byte[] content = message.getBytes(StandardCharsets.UTF_8);
            return new WebResourceResponse(
                    "text/plain",
                    "UTF-8",
                    403,
                    "Forbidden",
                    Collections.emptyMap(),
                    new ByteArrayInputStream(content)
            );
        }
    }
}
