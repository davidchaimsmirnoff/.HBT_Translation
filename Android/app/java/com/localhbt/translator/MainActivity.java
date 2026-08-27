package com.localhbt.translator;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.ContentResolver;
import android.content.Intent;
import android.content.SharedPreferences;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.provider.DocumentsContract;
import android.text.InputType;
import android.view.KeyEvent;
import android.view.ViewGroup;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.OutputStream;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;

/**
 * A shell around the translator's own web UI.
 *
 * Deliberately thin. Every screen, every button and every behaviour comes from
 * index.html on the PC, so the phone can never fall behind the desktop: there is
 * only one UI and this displays it. What the shell adds is the handful of things
 * a browser tab cannot do - writing files into a folder you picked on the phone,
 * remembering which machine to talk to, and surviving the tunnel going down
 * without dumping you on a Chrome error page.
 */
public class MainActivity extends Activity {

    /** Where the PC sits on the tailnet. Editable in-app; this is only the seed. */
    private static final String DEFAULT_SERVER = "http://100.104.64.110:8770";

    private static final String PREFS = "localhbt";
    private static final String K_SERVER = "server";
    private static final String K_TREE = "tree";

    private static final int REQ_TREE = 101;
    private static final int REQ_FILE = 102;

    private WebView web;
    private SharedPreferences prefs;
    private ValueCallback<Uri[]> filePicked;
    /** A save that arrived before a folder had been chosen, replayed after. */
    private String pendingBundle;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);

        web = new WebView(this);
        setContentView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        // IndexedDB is what holds the offline copy of every document. Without DOM
        // storage the whole cache-and-sync layer silently degrades to online-only.
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setBuiltInZoomControls(true);
        s.setDisplayZoomControls(false);
        s.setTextZoom(100);

        web.addJavascriptInterface(new Bridge(), "HBT");
        web.setWebViewClient(new Client());
        web.setWebChromeClient(new Chrome());
        // The web UI's last-resort save path is an <a download>; catch it so it
        // lands in a real folder rather than vanishing.
        web.setDownloadListener((url, agent, disp, mime, len) -> saveDataUrl(url, disp));

        if (state != null) web.restoreState(state);
        else if (prefs.contains(K_SERVER)) web.loadUrl(startUrl());
        else askServer(true);
    }

    @Override protected void onSaveInstanceState(Bundle b) { super.onSaveInstanceState(b); web.saveState(b); }

    @Override
    public boolean onKeyDown(int code, KeyEvent e) {
        if (code == KeyEvent.KEYCODE_BACK && web.canGoBack()) { web.goBack(); return true; }
        return super.onKeyDown(code, e);
    }

    private String server() { return prefs.getString(K_SERVER, DEFAULT_SERVER); }

    /**
     * Where to land on launch. The paste box is the desktop's use for this app;
     * on a phone you almost always want the document library, so ask for it -
     * unless the address already carries a query of its own.
     */
    private String startUrl() {
        String u = server();
        return u.contains("?") ? u : u + (u.endsWith("/") ? "?tab=docs" : "/?tab=docs");
    }

    // ---------------------------------------------------------------- server

    private void askServer(final boolean firstRun) {
        final EditText box = new EditText(this);
        box.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        box.setText(server());
        box.setSelection(box.getText().length());

        new AlertDialog.Builder(this)
                .setTitle(firstRun ? "Where is the translator?" : "Computer address")
                .setMessage("The Tailscale address of the PC running Run Translator.bat, "
                        + "including the port.")
                .setView(box)
                .setCancelable(!firstRun)
                .setPositiveButton("Connect", (d, w) -> {
                    String url = box.getText().toString().trim();
                    if (url.isEmpty()) url = DEFAULT_SERVER;
                    if (!url.startsWith("http")) url = "http://" + url;
                    prefs.edit().putString(K_SERVER, url).apply();
                    web.loadUrl(startUrl());
                })
                .setNegativeButton(firstRun ? "Use default" : "Cancel", (d, w) -> {
                    if (firstRun) {
                        prefs.edit().putString(K_SERVER, DEFAULT_SERVER).apply();
                        web.loadUrl(startUrl());
                    }
                })
                .show();
    }

    /**
     * Shown instead of Chrome's dinosaur when the PC is asleep or off the tailnet.
     * It is a local page, so it still has the HBT bridge and can offer the two
     * things worth offering here: try again, or point somewhere else.
     */
    private void showOffline(String detail) {
        String html =
                "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
                + "<style>body{font:16px/1.6 system-ui,sans-serif;margin:0;padding:34px 24px;"
                + "background:#0f1115;color:#e8eaef}"
                + "@media(prefers-color-scheme:light){body{background:#f6f7f9;color:#16181d}}"
                + "h1{font-size:19px;margin:0 0 10px}p{color:#9aa3b2}"
                + "code{font-size:14px}button{font:inherit;font-size:15px;margin:8px 8px 0 0;"
                + "padding:12px 18px;border-radius:10px;border:1px solid #3a4150;"
                + "background:#2f6fed;color:#fff;font-weight:600}"
                + "button.g{background:transparent;color:inherit}</style>"
                + "<h1>Can't reach the computer</h1>"
                + "<p>Tried <code>" + esc(server()) + "</code></p>"
                + "<p>Check that the PC is awake, that Tailscale is connected on both "
                + "devices, and that <b>Run Translator.bat</b> is running.</p>"
                + "<p style='font-size:13px'>" + esc(detail) + "</p>"
                + "<button onclick='HBT.retry()'>Try again</button>"
                + "<button class=g onclick='HBT.settings()'>Change address</button>";
        web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
    }

    private static String esc(String s) {
        return s == null ? "" : s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;");
    }

    // ---------------------------------------------------------------- bridge

    /** What index.html calls as window.HBT. */
    public class Bridge {

        /**
         * Write a whole document's files into the folder the user picked.
         *
         * Called from the web page's "Save to device". Runs on a binder thread,
         * not the UI thread, so the file IO here is fine where it stands.
         */
        @JavascriptInterface
        public String saveBundle(String json) {
            Uri tree = treeUri();
            if (tree == null) {
                pendingBundle = json;
                runOnUiThread(() -> MainActivity.this.pickFolder(true));
                return "choose a folder to save into";
            }
            try {
                return writeBundle(tree, new JSONObject(json));
            } catch (Exception e) {
                // A folder revoked or deleted since it was picked: ask again.
                pendingBundle = json;
                runOnUiThread(() -> MainActivity.this.pickFolder(true));
                return "that folder is no longer writable - pick another";
            }
        }

        @JavascriptInterface public void pickFolder() { runOnUiThread(() -> MainActivity.this.pickFolder(false)); }
        @JavascriptInterface public void settings()   { runOnUiThread(() -> askServer(false)); }
        @JavascriptInterface public void retry()      { runOnUiThread(() -> web.loadUrl(startUrl())); }
        @JavascriptInterface public String serverUrl(){ return server(); }
        @JavascriptInterface public String saveFolder(){
            Uri t = treeUri();
            return t == null ? "" : t.toString();
        }
        @JavascriptInterface
        public void keepAwake(final boolean on) {
            runOnUiThread(() -> {
                if (on) getWindow().addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                else getWindow().clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            });
        }
    }

    // ------------------------------------------------------------- SAF files

    private Uri treeUri() {
        String s = prefs.getString(K_TREE, null);
        if (s == null) return null;
        Uri u = Uri.parse(s);
        // A permission survives reboots only while the folder does. Verify.
        for (android.content.UriPermission p : getContentResolver().getPersistedUriPermissions())
            if (p.getUri().equals(u) && p.isWritePermission()) return u;
        return null;
    }

    private void pickFolder(boolean thenSave) {
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
        i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
        if (!thenSave) pendingBundle = null;
        try {
            startActivityForResult(i, REQ_TREE);
        } catch (ActivityNotFoundException e) {
            toast("No file manager on this phone to pick a folder with");
        }
    }

    private String writeBundle(Uri tree, JSONObject b) throws Exception {
        ContentResolver cr = getContentResolver();
        Uri root = DocumentsContract.buildDocumentUriUsingTree(
                tree, DocumentsContract.getTreeDocumentId(tree));

        String folderName = safeName(b.optString("folder", "LocalHBT"));
        Uri folder = findChild(cr, tree, root, folderName);
        if (folder == null)
            folder = DocumentsContract.createDocument(
                    cr, root, DocumentsContract.Document.MIME_TYPE_DIR, folderName);
        if (folder == null) throw new IllegalStateException("could not create " + folderName);

        JSONArray files = b.getJSONArray("files");
        int n = 0;
        for (int i = 0; i < files.length(); i++) {
            JSONObject f = files.getJSONObject(i);
            String name = safeName(f.getString("name"));
            // createDocument would otherwise leave a trail of "source (1).txt".
            Uri old = findChild(cr, tree, folder, name);
            if (old != null) DocumentsContract.deleteDocument(cr, old);

            Uri file = DocumentsContract.createDocument(cr, folder, "text/plain", name);
            if (file == null) continue;
            OutputStream os = cr.openOutputStream(file, "wt");
            OutputStreamWriter w = new OutputStreamWriter(os, StandardCharsets.UTF_8);
            w.write(f.optString("text", ""));
            w.flush();
            w.close();
            n++;
        }
        return "saved " + n + " file" + (n == 1 ? "" : "s") + " to " + folderName + "/";
    }

    private Uri findChild(ContentResolver cr, Uri tree, Uri parent, String name) {
        Uri kids = DocumentsContract.buildChildDocumentsUriUsingTree(
                tree, DocumentsContract.getDocumentId(parent));
        Cursor c = null;
        try {
            c = cr.query(kids, new String[]{
                    DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                    DocumentsContract.Document.COLUMN_DISPLAY_NAME}, null, null, null);
            while (c != null && c.moveToNext())
                if (name.equals(c.getString(1)))
                    return DocumentsContract.buildDocumentUriUsingTree(tree, c.getString(0));
        } catch (Exception ignored) {
        } finally {
            if (c != null) c.close();
        }
        return null;
    }

    /** Titles come from Hebrew filenames; keep the letters, drop what SAF rejects. */
    private static String safeName(String s) {
        String out = s.replaceAll("[\\\\/:*?\"<>|\\r\\n\\t]", "_").trim();
        if (out.length() > 90) out = out.substring(0, 90);
        return out.isEmpty() ? "LocalHBT" : out;
    }

    /** The download fallback: index.html hands us a blob: or data: URL. */
    private void saveDataUrl(String url, String disposition) {
        toast("Use ↓ Save to device - it writes all files at once");
    }

    // -------------------------------------------------------------- results

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);

        if (req == REQ_TREE) {
            if (res == RESULT_OK && data != null && data.getData() != null) {
                Uri tree = data.getData();
                getContentResolver().takePersistableUriPermission(tree,
                        Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
                prefs.edit().putString(K_TREE, tree.toString()).apply();
                if (pendingBundle != null) {
                    String json = pendingBundle;
                    pendingBundle = null;
                    try { toast(writeBundle(tree, new JSONObject(json))); }
                    catch (Exception e) { toast("could not save: " + e.getMessage()); }
                } else {
                    toast("saving into that folder from now on");
                }
            } else {
                pendingBundle = null;
            }
            return;
        }

        if (req == REQ_FILE) {
            if (filePicked == null) return;
            filePicked.onReceiveValue(
                    res == RESULT_OK ? WebChromeClient.FileChooserParams.parseResult(res, data) : null);
            filePicked = null;
        }
    }

    private void toast(String m) {
        runOnUiThread(() -> Toast.makeText(MainActivity.this, m, Toast.LENGTH_LONG).show());
    }

    // -------------------------------------------------------------- clients

    private class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
            Uri u = r.getUrl();
            String host = Uri.parse(server()).getHost();
            if (u.getHost() != null && u.getHost().equals(host)) return false;
            try { startActivity(new Intent(Intent.ACTION_VIEW, u)); }
            catch (ActivityNotFoundException e) { return false; }
            return true;
        }

        @Override
        public void onReceivedError(WebView v, WebResourceRequest r, WebResourceError e) {
            // Sub-resource failures are the page's problem, not ours; only a dead
            // main frame means the machine is unreachable.
            if (r.isForMainFrame()) showOffline(e.getDescription() == null ? "" : e.getDescription().toString());
        }
    }

    private class Chrome extends WebChromeClient {
        @Override
        public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb, FileChooserParams params) {
            if (filePicked != null) filePicked.onReceiveValue(null);
            filePicked = cb;
            try {
                startActivityForResult(params.createIntent(), REQ_FILE);
                return true;
            } catch (ActivityNotFoundException e) {
                filePicked = null;
                return false;
            }
        }
    }
}
