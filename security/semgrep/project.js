// Test cases for project.yml (semgrep --test security/semgrep).
function bad(el, value) {
  // ruleid: portal-html-sink
  el.innerHTML = value;
  // ruleid: portal-html-sink
  el.insertAdjacentHTML("beforeend", value);
  // ruleid: portal-html-sink
  eval(value);
}

function good(el, value) {
  // ok: portal-html-sink
  el.textContent = value;
  // ok: portal-html-sink
  el.replaceChildren(document.createTextNode(value));
}
