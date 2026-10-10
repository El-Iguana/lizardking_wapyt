// LizardKing's small browser helpers: things that are a few lines of DOM in
// JavaScript and awkward through Pyodide's proxies. Everything else is Python
// (appcode/lizardking.py).
(function () {
  "use strict";

  function supported(mime) {
    const ms = window.MediaSource;
    try {
      if (ms && ms.isTypeSupported(mime)) return true;
    } catch (e) { /* ignore */ }
    return document.createElement("video").canPlayType(mime) === "probably";
  }

  window.lizardking = {
    // What this browser decodes beyond the codecs every browser has. The
    // server decides direct play against this. Matroska needs the codecs in
    // the question: Chrome answers "maybe" to the bare type and "probably"
    // with them, and does play H.264/AAC .mkv files (measured). AC-3 in a
    // .mkv plays *silently* in Chrome, so ac3/eac3 must be asked separately.
    caps() {
      const v = document.createElement("video");
      return {
        hevc: supported('video/mp4; codecs="hvc1.1.6.L93.B0"') || supported('video/mp4; codecs="hev1.1.6.L93.B0"'),
        av1: supported('video/mp4; codecs="av01.0.05M.08"'),
        mkv: v.canPlayType('video/x-matroska; codecs="avc1.42E01E, mp4a.40.2"') === "probably",
        ac3: supported('audio/mp4; codecs="ac-3"'),
        eac3: supported('audio/mp4; codecs="ec-3"'),
      };
    },

    // Save the current frame as a PNG download. Same-origin and blob: media
    // never taint the canvas, so this works for direct and transcoded files.
    snapshot(video, name) {
      if (!video || !video.videoWidth) return false;
      const canvas = document.createElement("canvas");
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext("2d").drawImage(video, 0, 0);
      canvas.toBlob((blob) => {
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 10000);
      }, "image/png");
      return true;
    },

    // Fit (contain / cover / fill) and zoom of the picture.
    picture(video, fit, zoom) {
      if (!video) return;
      video.style.objectFit = fit || "";
      video.style.transform = zoom && zoom !== 1 ? `scale(${zoom})` : "";
    },
  };
})();
