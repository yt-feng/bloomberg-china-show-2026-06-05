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
    globals: [],
    assetIds: [],
    manifestUrls: [],
    m3u8Urls: []
  };

  function push(list, value) {
    if (value && !list.includes(value)) list.push(value);
  }

  function scanText(value) {
    if (!value) return;
    var text = String(value).split('\\\\u002F').join('/').split('\\\\u002f').join('/').split('\\\\/').join('/');
    var assetRe = /(?:assetId|assetID|asset_id)[^0-9a-fA-F]{0,40}([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/g;
    var manifestRe = /https?:\\/\\/[^\\s'<>]+media-manifest[^\\s'<>]*/g;
    var m3u8Re = /https?:\\/\\/[^\\s'<>]+?\\.m3u8(?:\\?[^\\s'<>]*)?/g;
    var match;
    while ((match = assetRe.exec(text)) !== null) push(out.assetIds, match[1].toLowerCase());
    while ((match = manifestRe.exec(text)) !== null) push(out.manifestUrls, match[0]);
    while ((match = m3u8Re.exec(text)) !== null) push(out.m3u8Urls, match[0]);
  }

  Array.from(document.querySelectorAll('script')).forEach(function (node) {
    var src = node.src || '';
    scanText(src);
    if (/m3u8|mpd|mp4|video|player|embed|jw|brightcove|media|bloomberg/i.test(src)) {
      push(out.scripts, src);
    }
    var text = node.textContent || '';
    scanText(text);
    if (/m3u8|mpd|mp4|video|player|embed|jw|brightcove|media|bloomberg/i.test(text)) {
      push(out.scripts, text.slice(0, 3000));
    }
  });

  Array.from(document.querySelectorAll('a[href], link[href], iframe[src]')).forEach(function (node) {
    var value = node.href || node.src;
    scanText(value);
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
    scanText(node.currentSrc);
    scanText(node.src);
    scanText(node.poster);
  });

  Array.from(document.querySelectorAll('source[src]')).forEach(function (node) {
    scanText(node.src);
    push(out.sources, node.src);
  });

  performance.getEntriesByType('resource').forEach(function (entry) {
    scanText(entry.name);
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
