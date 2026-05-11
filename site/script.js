const header = document.querySelector("[data-elevate]");

function syncHeaderElevation() {
  if (!header) return;
  header.classList.toggle("is-elevated", window.scrollY > 8);
}

syncHeaderElevation();
window.addEventListener("scroll", syncHeaderElevation, { passive: true });
