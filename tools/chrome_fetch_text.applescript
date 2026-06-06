on run argv
  set targetUrl to item 1 of argv
  tell application "Google Chrome"
    activate
    set js to "
      (function () {
        var target = '" & targetUrl & "';
        var xhr = new XMLHttpRequest();
        xhr.open('GET', target, false);
        xhr.withCredentials = true;
        xhr.setRequestHeader('Accept', 'application/vnd.apple.mpegurl, application/x-mpegURL, text/plain, */*');
        try {
          xhr.send(null);
          return JSON.stringify({
            status: xhr.status,
            statusText: xhr.statusText,
            responseURL: xhr.responseURL,
            headers: xhr.getAllResponseHeaders(),
            text: xhr.responseText
          });
        } catch (err) {
          return JSON.stringify({
            status: 0,
            statusText: String(err && err.message ? err.message : err),
            responseURL: '',
            headers: '',
            text: ''
          });
        }
      })();
    "
    return execute active tab of front window javascript js
  end tell
end run
