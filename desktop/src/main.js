import { mount } from "svelte";
import "./app.css";
import { applyTheme, savedTheme } from "./lib/theme.js";
import App from "./App.svelte";

applyTheme(savedTheme()); // before the first paint: no flash of the other theme

mount(App, { target: document.getElementById("app") });
