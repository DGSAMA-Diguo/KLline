package com.gupiap.kline;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;

import java.io.File;
import java.io.FileNotFoundException;

public final class UpdateProvider extends ContentProvider {
    public static final String AUTHORITY = "com.gupiap.kline.updates";
    public static final Uri UPDATE_URI =
            Uri.parse("content://" + AUTHORITY + "/package");

    private static final String MIME_TYPE =
            "application/vnd.android.package-archive";
    private static final String UPDATE_DIRECTORY = "updates";
    private static final String UPDATE_FILE = "KLineMobile-update.apk";

    public static File getUpdateFile(android.content.Context context) {
        return new File(
                new File(context.getCacheDir(), UPDATE_DIRECTORY),
                UPDATE_FILE
        );
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public String getType(Uri uri) {
        ensureAllowedUri(uri);
        return MIME_TYPE;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode)
            throws FileNotFoundException {
        ensureAllowedUri(uri);
        if (!"r".equals(mode)) {
            throw new FileNotFoundException("更新包只允许读取");
        }

        File file = getUpdateFile(requireProviderContext());
        if (!file.isFile()) {
            throw new FileNotFoundException("更新包不存在");
        }
        return ParcelFileDescriptor.open(
                file,
                ParcelFileDescriptor.MODE_READ_ONLY
        );
    }

    @Override
    public Cursor query(
            Uri uri,
            String[] projection,
            String selection,
            String[] selectionArgs,
            String sortOrder
    ) {
        ensureAllowedUri(uri);
        File file = getUpdateFile(requireProviderContext());
        String[] requested = projection == null
                ? new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE}
                : projection;
        MatrixCursor cursor = new MatrixCursor(requested, 1);
        MatrixCursor.RowBuilder row = cursor.newRow();
        for (String column : requested) {
            if (OpenableColumns.DISPLAY_NAME.equals(column)) {
                row.add(UPDATE_FILE);
            } else if (OpenableColumns.SIZE.equals(column)) {
                row.add(file.isFile() ? file.length() : 0L);
            } else {
                row.add(null);
            }
        }
        return cursor;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        throw new UnsupportedOperationException("不允许写入更新包");
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException("不允许删除更新包");
    }

    @Override
    public int update(
            Uri uri,
            ContentValues values,
            String selection,
            String[] selectionArgs
    ) {
        throw new UnsupportedOperationException("不允许修改更新包");
    }

    private android.content.Context requireProviderContext() {
        android.content.Context context = getContext();
        if (context == null) {
            throw new IllegalStateException("更新组件尚未初始化");
        }
        return context;
    }

    private static void ensureAllowedUri(Uri uri) {
        if (uri == null
                || !"content".equals(uri.getScheme())
                || !AUTHORITY.equals(uri.getAuthority())
                || !"/package".equals(uri.getPath())) {
            throw new SecurityException("拒绝访问未知更新文件");
        }
    }
}
