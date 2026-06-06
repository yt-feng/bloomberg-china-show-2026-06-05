on run argv
  tell application "Google Chrome"
    activate
    set js to "
      (function () {
  var out = {
    url: location.href,
    title: document.title,
    ready: document.readyState,
    textSample: document.documentElement.innerText.slice(0, 1200),
    scripts: [],
    links: [],
    videos: [],
    sources: [],
    performance: [],
    globals: []
  };

  function push(list, value) {
    if (value && !list.includes(value)) list.push(value);
  }

  Array.from(document.querySelectorAll('script')).forEach(function (node) {
    var src = node.src || '';
    if (/m3u8|mpd|mp4|video|player|embed|jw|brightcove|media|bloomberg/i.test(src)) {
      push(out.scripts, src);
    }
    var text = node.textContent || '';
    if (/m3u8|mpd|mp4|video|player|embed|jw|brightcove|media|bloomberg/i.test(text)) {
      push(out.scripts, text.slice(0, 3000));
    }
  });

  Array.from(document.querySelectorAll('a[href], link[href], iframe[src]')).forEach(function (node) {
    var value = node.href || node.src;
    if (/m3u8|mpd|mp4|video|player|embed|jw|brightcove|media|bloomberg/i.test(value)) {
      push(out.links, value);
    }
  });

  Array.from(document.querySelectorAll('video')).forEach(function (node) {
    out.videos.push({
      currentSrc: node.currentSrc,
      src: node.src,
      duration: Number.isFinite(node.duration) ? node.duration : null,
      poster: node.poster
    });
  });

  Array.from(document.querySelectorAll('source[src]')).forEach(function (node) {
    push(out.sources, node.src);
  });

  performance.getEntriesByType('resource').forEach(function (entry) {
    if (/m3u8|mpd|mp4|\\.ts(\\?|$)|\\.m4s(\\?|$)|video|player|embed|jw|brightcove|media/i.test(entry.name)) {
      push(out.performance, entry.name);
    }
  });

  Object.keys(window).forEach(function (key) {
    if (/video|player|media|jw|brightcove|bloomberg/i.test(key)) {
      push(out.globals, key);
    }
  });

  return JSON.stringify(out, null, 2);
})();
  "
    return execute active tab of front window javascript js
  end tell
end run
