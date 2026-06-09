import re
with open('pocket-arya-store-new/src/styles.css', 'r', encoding='utf-8') as f:
    css = f.read()

# Pattern to find from GET EPISODE BUTTON to the end of the file
pattern = r'/\* --------------------------------------------------\s*GET EPISODE BUTTON \(Purchased State\).*'

new_css = '''/* --------------------------------------------------
   GET EPISODE BUTTON (Purchased State)
   -------------------------------------------------- */
.uiverse-get-episode-btn {
  background: transparent;
  color: var(--background);
  font-family: inherit;
  padding: 0.35em;
  padding-left: 1.2em;
  font-size: 14px;
  font-weight: 700;
  border-radius: 5px;
  border: none;
  letter-spacing: 0.05em;
  display: flex;
  align-items: center;
  position: relative;
  height: 44px;
  padding-right: 3.3em;
  cursor: pointer;
  transition: all 0.3s;
  z-index: 1;
}

.uiverse-get-episode-btn::before {
  content: "";
  position: absolute;
  inset: 0;
  background-color: var(--foreground);
  border-radius: 5px;
  z-index: -1;
  box-shadow: 0 4px 14px rgba(0,0,0,0.1);
  transition: all 0.3s;
}

.uiverse-get-episode-btn .icon {
  background: var(--background);
  color: var(--foreground);
  margin-left: 1em;
  position: absolute;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 2.2em;
  width: 2.2em;
  border-radius: 5px;
  right: 0.3em;
  transition: all 0.3s;
  z-index: 10;
}

.uiverse-get-episode-btn:hover .icon {
  width: calc(100% - 0.6em);
}

.uiverse-get-episode-btn .icon svg {
  width: 1.1em;
  transition: transform 0.3s;
  color: var(--foreground);
}

.uiverse-get-episode-btn:hover .icon svg {
  transform: translateX(0.1em);
}

.uiverse-get-episode-btn:active .icon {
  transform: scale(0.95);
}

/* Theme overrides */
.theme-cream .uiverse-get-episode-btn::before {
  background: #000;
  border: 1px solid #000;
  box-shadow: 4px 4px 0px #000;
}
.theme-cream .uiverse-get-episode-btn .icon {
  background: #fff;
  color: #000;
  border: 1px solid #000;
}
.theme-mint .uiverse-get-episode-btn::before {
  background: #1fbf7a;
}
.theme-mint .uiverse-get-episode-btn .icon {
  background: #fff;
  color: #1fbf7a;
}

/* --------------------------------------------------
   HOME/EXPLORE SERIES BUTTON (Purchased State)
   -------------------------------------------------- */
.uiverse-home-series-btn {
  display: flex;
  width: 100%;
  height: 34px;
  background-color: transparent;
  border-radius: 5px;
  justify-content: space-between;
  align-items: center;
  border: none;
  cursor: pointer;
  padding-left: 8px;
  position: relative;
  z-index: 1;
}

.uiverse-home-series-btn::before {
  content: "";
  position: absolute;
  inset: 0;
  background-color: var(--foreground);
  border-radius: 5px;
  box-shadow: 0px 2px 5px rgba(0,0,0,0.1);
  z-index: -1;
  transition: all 0.3s;
}

.uiverse-home-series-btn .icon-Container {
  width: 34px;
  height: 34px;
  background-color: var(--background);
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 5px;
  border: 2px solid var(--foreground);
  color: var(--foreground);
  z-index: 10;
}
.uiverse-home-series-btn .text {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--background);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  z-index: 10;
}
.uiverse-home-series-btn .icon-Container svg {
  transition-duration: 1.5s;
  width: 12px;
  height: 14px;
}
.uiverse-home-series-btn:hover .icon-Container svg {
  transition-duration: 1.5s;
  animation: home-arrow 1s linear infinite;
}
@keyframes home-arrow {
  0% { opacity: 0; transform: translateX(0px); }
  100% { opacity: 1; transform: translateX(5px); }
}

/* Theme Overrides */
.theme-cream .uiverse-home-series-btn::before {
  background-color: #000;
  border: 1px solid #000;
  box-shadow: 2px 2px 0px #000;
}
.theme-cream .uiverse-home-series-btn .icon-Container {
  background-color: #fff;
  border: 1px solid #000;
  color: #000;
  width: 32px;
  height: 32px;
}
.theme-mint .uiverse-home-series-btn::before {
  background-color: #1fbf7a;
}
.theme-mint .uiverse-home-series-btn .icon-Container {
  background-color: #fff;
  border-color: #1fbf7a;
  color: #1fbf7a;
}

/* --------------------------------------------------
   ROTATING BEAM BORDER (Used in Purchased State Buttons)
   -------------------------------------------------- */
.dots_border {
  --size_border: calc(100% + 4px);
  overflow: hidden;
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: var(--size_border);
  height: var(--size_border);
  background-color: transparent;
  border-radius: 6px;
  z-index: -2;
  pointer-events: none;
}

.dots_border::before {
  content: "";
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  transform-origin: left;
  width: 100%;
  height: 2rem;
  background-color: var(--foreground);
  mask: linear-gradient(transparent 0%, white 120%);
  -webkit-mask: linear-gradient(transparent 0%, white 120%);
  animation: rotate-beam 2s linear infinite;
}

@keyframes rotate-beam {
  0% { transform: translate(-50%, -50%) rotate(0deg); }
  100% { transform: translate(-50%, -50%) rotate(360deg); }
}

/* Theme Overrides for Beam */
.theme-cream .dots_border::before {
  background-color: #000;
}
.theme-mint .dots_border::before {
  background-color: #1fbf7a;
}
'''

new_content = re.sub(pattern, new_css, css, flags=re.DOTALL)
with open('pocket-arya-store-new/src/styles.css', 'w', encoding='utf-8') as f:
    f.write(new_content)
