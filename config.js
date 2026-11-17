// TNxFlix Web — backend base URL.
//
// "" (empty) means "same origin": use this when the FastAPI backend
// serves this page itself (single-host deploy — Hugging Face / Render
// running the whole tnxflix-web folder).
//
// GitHub Pages split deploy: the pages live on GitHub Pages and the
// API/proxy runs on a separate free host. Set the backend's full URL
// here (no trailing slash), e.g.:
//   window.TNXFLIX_API = "https://your-name-tnxflix.hf.space";
window.TNXFLIX_API = "https://tnxflix-api.onrender.com";
