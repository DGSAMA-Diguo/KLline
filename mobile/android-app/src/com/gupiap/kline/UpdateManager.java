package com.gupiap.kline;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.ProgressDialog;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

@SuppressWarnings("deprecation")
public final class UpdateManager {
    private static final String UPDATE_MANIFEST_URL =
            "https://gitee.com/dgsproject/kline/raw/master/update/update.json";
    private static final String UPDATE_HOST = "gitee.com";
    private static final String UPDATE_RAW_HOST = "raw.giteeusercontent.com";
    private static final String UPDATE_PATH_PREFIX =
            "/dgsproject/kline/raw/master/update/";
    private static final int INSTALL_PERMISSION_REQUEST = 7001;
    private static final int CONNECT_TIMEOUT_MS = 12_000;
    private static final int READ_TIMEOUT_MS = 30_000;
    private static final int MAX_REDIRECTS = 4;
    private static final int MAX_MANIFEST_BYTES = 64 * 1024;
    private static final int MAX_UPDATE_PARTS = 32;
    private static final long MAX_PART_BYTES = 9L * 1024L * 1024L;
    private static final long MAX_APK_BYTES = 200L * 1024L * 1024L;

    private final Activity activity;
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final AtomicBoolean operationRunning = new AtomicBoolean(false);

    private ProgressDialog progressDialog;
    private boolean destroyed;
    private boolean waitingForInstallPermission;

    public UpdateManager(Activity activity) {
        this.activity = activity;
    }

    public void checkForUpdates() {
        if (!operationRunning.compareAndSet(false, true)) {
            return;
        }

        executor.execute(() -> {
            try {
                UpdateInfo info = fetchUpdateInfo();
                if (info.versionCode > getInstalledVersionCode()) {
                    activity.runOnUiThread(() -> showUpdateDialog(info));
                }
            } catch (Exception ignored) {
                // 自动检测失败时不影响离线功能，下一次启动会再次检测。
            } finally {
                operationRunning.set(false);
            }
        });
    }

    public void onActivityResult(int requestCode) {
        if (requestCode != INSTALL_PERMISSION_REQUEST
                || !waitingForInstallPermission) {
            return;
        }
        waitingForInstallPermission = false;
        if (activity.getPackageManager().canRequestPackageInstalls()) {
            launchInstaller();
        } else {
            showToast("未获得安装权限，暂时无法更新");
        }
    }

    public void destroy() {
        destroyed = true;
        executor.shutdownNow();
        dismissProgress();
    }

    private UpdateInfo fetchUpdateInfo()
            throws IOException, JSONException {
        HttpURLConnection connection = openTrustedConnection(
                UPDATE_MANIFEST_URL,
                "application/json"
        );
        try (InputStream input = new BufferedInputStream(
                connection.getInputStream()
        )) {
            byte[] content = readLimited(input, MAX_MANIFEST_BYTES);
            JSONObject json = new JSONObject(
                    new String(content, StandardCharsets.UTF_8)
            );
            return UpdateInfo.fromJson(json);
        } finally {
            connection.disconnect();
        }
    }

    private void showUpdateDialog(UpdateInfo info) {
        if (!canShowUi()) {
            return;
        }

        StringBuilder message = new StringBuilder()
                .append("发现新版本：")
                .append(info.versionName);
        if (!info.notes.isEmpty()) {
            message.append("\n\n").append(info.notes);
        }

        new AlertDialog.Builder(activity)
                .setTitle("发现应用更新")
                .setMessage(message.toString())
                .setNegativeButton("稍后", null)
                .setPositiveButton(
                        "立即更新",
                        (dialog, which) -> downloadUpdate(info)
                )
                .show();
    }

    private void downloadUpdate(UpdateInfo info) {
        if (!operationRunning.compareAndSet(false, true)) {
            return;
        }
        showDownloadProgress();

        executor.execute(() -> {
            File temporaryFile = getTemporaryUpdateFile();
            try {
                downloadApk(info, temporaryFile);
                verifySha256(temporaryFile, info.sha256);
                verifyDownloadedPackage(temporaryFile, info);
                moveVerifiedPackage(temporaryFile);
                activity.runOnUiThread(() -> {
                    dismissProgress();
                    requestInstall();
                });
            } catch (Exception error) {
                if (temporaryFile.exists()) {
                    temporaryFile.delete();
                }
                activity.runOnUiThread(() -> {
                    dismissProgress();
                    showToast("更新失败：" + safeMessage(error));
                });
            } finally {
                operationRunning.set(false);
            }
        });
    }

    private void downloadApk(UpdateInfo info, File target)
            throws IOException, NoSuchAlgorithmException {
        File parent = target.getParentFile();
        if (parent == null || (!parent.isDirectory() && !parent.mkdirs())) {
            throw new IOException("无法创建更新目录");
        }
        if (target.exists() && !target.delete()) {
            throw new IOException("无法清理旧更新文件");
        }

        long total = 0;
        try (
                BufferedOutputStream output = new BufferedOutputStream(
                        new FileOutputStream(target)
                )
        ) {
            // 码云匿名下载限制大文件，分片下载后按清单顺序合并。
            for (UpdatePart part : info.parts) {
                total = downloadPart(part, output, total, info.size);
            }
        }

        if (total <= 0) {
            throw new IOException("更新包内容为空");
        }
        if (total != info.size) {
            throw new IOException("更新包大小不一致");
        }
    }

    private long downloadPart(
            UpdatePart part,
            BufferedOutputStream output,
            long completed,
            long expectedTotal
    ) throws IOException, NoSuchAlgorithmException {
        HttpURLConnection connection = openTrustedConnection(
                part.url,
                "application/octet-stream"
        );
        long contentLength = connection.getContentLengthLong();
        if (contentLength >= 0 && contentLength != part.size) {
            connection.disconnect();
            throw new IOException("更新分片大小不一致");
        }

        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        long partTotal = 0;
        try (InputStream input = new BufferedInputStream(
                connection.getInputStream()
        )) {
            byte[] buffer = new byte[64 * 1024];
            int count;
            while ((count = input.read(buffer)) != -1) {
                if (Thread.currentThread().isInterrupted()) {
                    throw new IOException("更新已取消");
                }
                partTotal += count;
                if (partTotal > part.size
                        || completed + partTotal > MAX_APK_BYTES) {
                    throw new IOException("更新分片大小超过限制");
                }
                output.write(buffer, 0, count);
                digest.update(buffer, 0, count);
                updateProgress(completed + partTotal, expectedTotal);
            }
        } finally {
            connection.disconnect();
        }

        if (partTotal != part.size) {
            throw new IOException("更新分片大小不一致");
        }
        if (!toHex(digest.digest()).equals(part.sha256)) {
            throw new IOException("更新分片完整性校验失败");
        }
        return completed + partTotal;
    }

    private void verifyDownloadedPackage(File file, UpdateInfo info)
            throws PackageManager.NameNotFoundException, IOException {
        PackageManager manager = activity.getPackageManager();
        int flags = PackageManager.GET_SIGNATURES;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            flags |= PackageManager.GET_SIGNING_CERTIFICATES;
        }
        PackageInfo archive = manager.getPackageArchiveInfo(
                file.getAbsolutePath(),
                flags
        );
        if (archive == null) {
            throw new IOException("无法识别更新包");
        }
        if (!activity.getPackageName().equals(archive.packageName)) {
            throw new IOException("更新包名称不匹配");
        }

        long archiveVersion = getVersionCode(archive);
        if (archiveVersion != info.versionCode
                || archiveVersion <= getInstalledVersionCode()) {
            throw new IOException("更新包版本不匹配");
        }

        PackageInfo installed = manager.getPackageInfo(
                activity.getPackageName(),
                flags
        );
        if (!signatureDigests(installed).equals(signatureDigests(archive))) {
            throw new IOException("更新包签名不匹配");
        }
    }

    private void requestInstall() {
        if (!canShowUi()) {
            return;
        }
        if (activity.getPackageManager().canRequestPackageInstalls()) {
            launchInstaller();
            return;
        }

        new AlertDialog.Builder(activity)
                .setTitle("允许安装应用更新")
                .setMessage("请在系统设置中允许此应用安装更新，返回后会继续安装。")
                .setNegativeButton("取消", null)
                .setPositiveButton("打开设置", (dialog, which) -> {
                    waitingForInstallPermission = true;
                    Intent intent = new Intent(
                            Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                            Uri.parse("package:" + activity.getPackageName())
                    );
                    activity.startActivityForResult(
                            intent,
                            INSTALL_PERMISSION_REQUEST
                    );
                })
                .show();
    }

    private void launchInstaller() {
        File updateFile = UpdateProvider.getUpdateFile(activity);
        if (!updateFile.isFile()) {
            showToast("已校验的更新包不存在");
            return;
        }

        Intent intent = new Intent(Intent.ACTION_VIEW);
        intent.setDataAndType(
                UpdateProvider.UPDATE_URI,
                "application/vnd.android.package-archive"
        );
        intent.setClipData(
                ClipData.newRawUri("应用更新包", UpdateProvider.UPDATE_URI)
        );
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        try {
            activity.startActivity(intent);
        } catch (ActivityNotFoundException error) {
            showToast("系统中没有可用的安装程序");
        }
    }

    private HttpURLConnection openTrustedConnection(
            String address,
            String accept
    ) throws IOException {
        URL current = new URL(address);
        for (int redirect = 0; redirect <= MAX_REDIRECTS; redirect++) {
            validateUpdateUrl(current);
            HttpURLConnection connection =
                    (HttpURLConnection) current.openConnection();
            connection.setConnectTimeout(CONNECT_TIMEOUT_MS);
            connection.setReadTimeout(READ_TIMEOUT_MS);
            connection.setInstanceFollowRedirects(false);
            connection.setUseCaches(false);
            connection.setRequestMethod("GET");
            connection.setRequestProperty("Accept", accept);
            connection.setRequestProperty("Accept-Encoding", "identity");
            connection.setRequestProperty("Cache-Control", "no-cache, no-store");
            connection.setRequestProperty("Pragma", "no-cache");
            connection.setRequestProperty(
                    "User-Agent",
                    "KLineMobile-Android-Updater"
            );

            int status = connection.getResponseCode();
            if (status == HttpURLConnection.HTTP_OK) {
                return connection;
            }
            if (status == HttpURLConnection.HTTP_MOVED_PERM
                    || status == HttpURLConnection.HTTP_MOVED_TEMP
                    || status == HttpURLConnection.HTTP_SEE_OTHER
                    || status == 307
                    || status == 308) {
                String location = connection.getHeaderField("Location");
                connection.disconnect();
                if (location == null || location.trim().isEmpty()) {
                    throw new IOException("更新地址重定向无效");
                }
                current = new URL(current, location);
                continue;
            }

            connection.disconnect();
            throw new IOException("更新服务器返回状态：" + status);
        }
        throw new IOException("更新地址重定向次数过多");
    }

    private static void validateUpdateUrl(URL url) throws IOException {
        boolean primaryHost = UPDATE_HOST.equalsIgnoreCase(url.getHost());
        boolean rawHost = UPDATE_RAW_HOST.equalsIgnoreCase(url.getHost());
        if (!"https".equalsIgnoreCase(url.getProtocol())
                || (!primaryHost && !rawHost)
                || (url.getPort() != -1 && url.getPort() != 443)
                || url.getUserInfo() != null
                || (primaryHost && url.getQuery() != null)
                || url.getRef() != null
                || !url.getPath().startsWith(UPDATE_PATH_PREFIX)
                || url.getPath().contains("..")) {
            throw new IOException("更新地址不在允许范围内");
        }
    }

    private static byte[] readLimited(InputStream input, int limit)
            throws IOException {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[4096];
        int count;
        int total = 0;
        while ((count = input.read(buffer)) != -1) {
            total += count;
            if (total > limit) {
                throw new IOException("版本信息大小超过限制");
            }
            output.write(buffer, 0, count);
        }
        return output.toByteArray();
    }

    private void verifySha256(File file, String expected)
            throws IOException, NoSuchAlgorithmException {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (InputStream input = new BufferedInputStream(
                new FileInputStream(file)
        )) {
            byte[] buffer = new byte[64 * 1024];
            int count;
            while ((count = input.read(buffer)) != -1) {
                digest.update(buffer, 0, count);
            }
        }

        String actual = toHex(digest.digest());
        if (!actual.equals(expected)) {
            throw new IOException("更新包完整性校验失败");
        }
    }

    private Set<String> signatureDigests(PackageInfo info)
            throws IOException {
        Signature[] signatures = null;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
                && info.signingInfo != null) {
            signatures = info.signingInfo.getApkContentsSigners();
        }
        if (signatures == null || signatures.length == 0) {
            // 部分安卓定制系统不会为外部安装包填充新版签名字段。
            signatures = info.signatures;
        }
        if (signatures == null || signatures.length == 0) {
            throw new IOException("更新包缺少签名");
        }

        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            Set<String> result = new HashSet<>();
            for (Signature signature : signatures) {
                result.add(toHex(digest.digest(signature.toByteArray())));
                digest.reset();
            }
            return result;
        } catch (NoSuchAlgorithmException error) {
            throw new IOException("系统不支持签名校验", error);
        }
    }

    private long getInstalledVersionCode()
            throws PackageManager.NameNotFoundException {
        PackageInfo info = activity.getPackageManager().getPackageInfo(
                activity.getPackageName(),
                0
        );
        return getVersionCode(info);
    }

    private static long getVersionCode(PackageInfo info) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            return info.getLongVersionCode();
        }
        return info.versionCode;
    }

    private File getTemporaryUpdateFile() {
        File verified = UpdateProvider.getUpdateFile(activity);
        return new File(verified.getParentFile(), "KLineMobile-update.part.apk");
    }

    private void moveVerifiedPackage(File temporary) throws IOException {
        File verified = UpdateProvider.getUpdateFile(activity);
        if (verified.exists() && !verified.delete()) {
            throw new IOException("无法替换旧更新包");
        }
        if (!temporary.renameTo(verified)) {
            throw new IOException("无法保存已校验更新包");
        }
    }

    private void showDownloadProgress() {
        if (!canShowUi()) {
            return;
        }
        progressDialog = new ProgressDialog(activity);
        progressDialog.setTitle("正在更新");
        progressDialog.setMessage("正在下载并校验安装包");
        progressDialog.setProgressStyle(ProgressDialog.STYLE_HORIZONTAL);
        progressDialog.setMax(100);
        progressDialog.setProgress(0);
        progressDialog.setCancelable(false);
        progressDialog.show();
    }

    private void updateProgress(long current, long expected) {
        if (expected <= 0) {
            return;
        }
        int percent = (int) Math.min(100L, current * 100L / expected);
        activity.runOnUiThread(() -> {
            if (progressDialog != null && progressDialog.isShowing()) {
                progressDialog.setProgress(percent);
            }
        });
    }

    private void dismissProgress() {
        if (progressDialog != null) {
            if (progressDialog.isShowing()) {
                progressDialog.dismiss();
            }
            progressDialog = null;
        }
    }

    private boolean canShowUi() {
        return !destroyed
                && !activity.isFinishing()
                && !activity.isDestroyed();
    }

    private void showToast(String message) {
        if (canShowUi()) {
            Toast.makeText(activity, message, Toast.LENGTH_LONG).show();
        }
    }

    private static String safeMessage(Exception error) {
        String message = error.getMessage();
        return message == null || message.trim().isEmpty()
                ? "未知错误"
                : message;
    }

    private static String toHex(byte[] bytes) {
        StringBuilder result = new StringBuilder(bytes.length * 2);
        for (byte value : bytes) {
            result.append(String.format(Locale.ROOT, "%02x", value & 0xff));
        }
        return result.toString();
    }

    private static final class UpdateInfo {
        private final long versionCode;
        private final String versionName;
        private final String sha256;
        private final String notes;
        private final long size;
        private final List<UpdatePart> parts;

        private UpdateInfo(
                long versionCode,
                String versionName,
                String sha256,
                String notes,
                long size,
                List<UpdatePart> parts
        ) {
            this.versionCode = versionCode;
            this.versionName = versionName;
            this.sha256 = sha256;
            this.notes = notes;
            this.size = size;
            this.parts = parts;
        }

        private static UpdateInfo fromJson(JSONObject json)
                throws JSONException {
            long versionCode = json.getLong("version_code");
            String versionName = json.getString("version_name").trim();
            String sha256 = json.getString("sha256")
                    .trim()
                    .toLowerCase(Locale.ROOT);
            String notes = json.optString("notes", "")
                    .replace("\r", "")
                    .trim();
            long size = json.optLong("size", -1L);
            JSONArray partArray = json.getJSONArray("parts");

            if (versionCode <= 0) {
                throw new JSONException("版本编号无效");
            }
            if (versionName.isEmpty() || versionName.length() > 40) {
                throw new JSONException("版本名称无效");
            }
            if (!sha256.matches("[0-9a-f]{64}")) {
                throw new JSONException("文件校验值无效");
            }
            if (notes.length() > 2000) {
                throw new JSONException("更新说明过长");
            }
            if (size <= 0 || size > MAX_APK_BYTES) {
                throw new JSONException("更新包大小无效");
            }
            if (partArray.length() <= 0
                    || partArray.length() > MAX_UPDATE_PARTS) {
                throw new JSONException("更新分片数量无效");
            }

            List<UpdatePart> parts = new ArrayList<>(partArray.length());
            Set<String> partUrls = new HashSet<>();
            long totalSize = 0;
            for (int index = 0; index < partArray.length(); index++) {
                UpdatePart part = UpdatePart.fromJson(
                        partArray.getJSONObject(index)
                );
                if (!partUrls.add(part.url)) {
                    throw new JSONException("更新分片地址重复");
                }
                if (totalSize > MAX_APK_BYTES - part.size) {
                    throw new JSONException("更新分片总大小超过限制");
                }
                totalSize += part.size;
                parts.add(part);
            }
            if (totalSize != size) {
                throw new JSONException("更新分片总大小不一致");
            }

            return new UpdateInfo(
                    versionCode,
                    versionName,
                    sha256,
                    notes,
                    size,
                    Collections.unmodifiableList(parts)
            );
        }
    }

    private static final class UpdatePart {
        private final String url;
        private final long size;
        private final String sha256;

        private UpdatePart(String url, long size, String sha256) {
            this.url = url;
            this.size = size;
            this.sha256 = sha256;
        }

        private static UpdatePart fromJson(JSONObject json)
                throws JSONException {
            String url = json.getString("url").trim();
            long size = json.optLong("size", -1L);
            String sha256 = json.getString("sha256")
                    .trim()
                    .toLowerCase(Locale.ROOT);

            if (size <= 0 || size > MAX_PART_BYTES) {
                throw new JSONException("更新分片大小无效");
            }
            if (!sha256.matches("[0-9a-f]{64}")) {
                throw new JSONException("更新分片校验值无效");
            }
            try {
                validateUpdateUrl(new URL(url));
            } catch (IOException error) {
                throw new JSONException("更新分片地址无效");
            }
            return new UpdatePart(url, size, sha256);
        }
    }
}
