<script>
  // AI-written text: always through render() (src/lib/render.js). Links open in the browser after the shell asks.
  import { render } from "../lib/render.js";
  import { openLink } from "../lib/shell.js";

  let { text = "" } = $props();
  const html = $derived(render(text));

  function follow(e) {
    const a = e.target.closest?.("a[data-href]");
    if (!a || (e.type === "keydown" && e.key !== "Enter")) return;
    e.preventDefault();
    openLink(a.getAttribute("data-href"));
  }
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="prose" onclick={follow} onkeydown={follow}>{@html html}</div>
