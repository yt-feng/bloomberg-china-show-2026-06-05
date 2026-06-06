on run argv
  set targetUrl to item 1 of argv
  tell application "Google Chrome"
    activate
    set js to "
      (function () {
        var target = '" & targetUrl & "';
        var xhr = new XMLHttpRequest();
        xhr.open('GET', target, false);
        xhr.withCredentials = false;
        try {
          xhr.send(null);
          return JSON.stringify({
            status: xhr.status,
            responseURL: xhr.responseURL,
            text: xhr.responseText
          });
        } catch (err) {
          return JSON.stringify({
            status: 0,
            responseURL: '',
            text: String(err && err.message ? err.message : err)
          });
        }
      })();
    "
    return execute active tab of front window javascript js
  end tell
end run
