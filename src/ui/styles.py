"""
UI Styles and Theme Configuration
Contains all CSS styling for the Streamlit application
"""


def get_light_theme_css() -> str:
    """Return CSS for a clean light theme that keeps your layout and icons."""
    return """
<style>
    
    /* Main background */
    .stApp {
        background-color: #ffffff;
    }
    
    /* Main content area */
    .main .block-container {
        background-color: #ffffff;
        padding-top: 2rem;
    }
    
    /* Text colors */
    h1, h2, h3, h4, h5, h6, p, label, .stMarkdown {
        color: #111111 !important;
    }
    
    /* Streamlit widgets */
    .stTextInput > div > div > input {
        background-color: #ffffff !important;
        color: #111111 !important;
        border: 1px solid #cccccc !important;
        border-radius: 4px !important;
        padding: 8px 12px !important;
    }
    
    .stTextInput > div > div > input:focus {
        background-color: #f5f5f5 !important;
        border-color: #0066cc !important;
        outline: none !important;
    }
    
    .stTextInput > label {
        color: #111111 !important;
        font-weight: 500 !important;
    }
    
    /* Selectbox styling - make it more visible and user-friendly */
    .stSelectbox > div > div > select {
        background-color: #ffffff !important;
        color: #111111 !important;
        border: 1px solid #cccccc !important;
        border-radius: 4px !important;
        padding: 8px 12px !important;
    }
    
    .stSelectbox > div > div > select:hover {
        background-color: #f5f5f5 !important;
        border-color: #bbbbbb !important;
    }
    
    .stSelectbox > div > div > select:focus {
        background-color: #f5f5f5 !important;
        border-color: #0066cc !important;
        outline: none !important;
    }
    
    /* Selectbox label */
    .stSelectbox > label {
        color: #111111 !important;
        font-weight: 500 !important;
    }
    
    /* Selectbox container */
    [data-testid="stSelectbox"] {
        color: #111111 !important;
    }

    [data-testid="stSelectbox"] > div > div {
        background-color: #ffffff !important;
    }

    /* Selectbox dropdown popup (BaseWeb popover) */
    [data-baseweb="popover"],
    [data-baseweb="popover"] * {
        background-color: #ffffff !important;
        color: #111111 !important;
    }

    /* Selectbox dropdown menu list */
    [data-baseweb="menu"],
    [data-baseweb="menu"] ul,
    [data-baseweb="menu"] li {
        background-color: #ffffff !important;
        color: #111111 !important;
    }

    /* Each dropdown option — normal state */
    [role="option"] {
        background-color: #ffffff !important;
        color: #111111 !important;
    }

    /* Dropdown option — hover state */
    [role="option"]:hover,
    [data-baseweb="menu"] li:hover {
        background-color: #e8f0fe !important;
        color: #111111 !important;
    }

    /* Dropdown option — selected/highlighted state */
    [role="option"][aria-selected="true"],
    [data-baseweb="menu"] li[aria-selected="true"] {
        background-color: #cce0ff !important;
        color: #111111 !important;
    }

    /* Selectbox selected value display text */
    [data-testid="stSelectbox"] [data-baseweb="select"] div,
    [data-testid="stSelectbox"] [data-baseweb="select"] span {
        color: #111111 !important;
        background-color: #ffffff !important;
    }
    
    /* Buttons - make them bright and primary (orange) */
    .stButton > button {
        background-color: #ff8800 !important;
        color: #ffffff !important;
        border: 1px solid #ff9900 !important;
        border-radius: 4px !important;
        font-weight: 500 !important;
        padding: 10px 20px !important;
        transition: all 0.2s ease !important;
    }
    
    .stButton > button:hover {
        background-color: #ff9f26 !important;
        border-color: #ffb347 !important;
        transform: translateY(-1px) !important;
        box-shadow: 0 2px 4px rgba(255, 136, 0, 0.4) !important;
    }
    
    .stButton > button:active {
        background-color: #e67600 !important;
        transform: translateY(0) !important;
    }
    
    /* Download buttons - outlined style */
    .stDownloadButton > button {
        background-color: transparent !important;
        color: #0066cc !important;
        border: 1px solid #0066cc !important;
        font-weight: 500 !important;
    }

    .stDownloadButton > button:hover {
        background-color: #e8f0fe !important;
        border-color: #0052a3 !important;
        color: #0052a3 !important;
    }

    .stDownloadButton > button:active {
        background-color: #d0e3fc !important;
    }
    
    /* File uploader - ULTRA comprehensive dark theme styling */
    .uploadedFile {
        background-color: #f5f5f5 !important;
        color: #111111 !important;
    }
    
    /* File uploader container - simple light styling */
    [data-testid="stFileUploader"] > div {
        background-color: #f8f9fa !important;
        border-radius: 6px !important;
        border: 1px dashed #cccccc !important;
    }
    
    [data-baseweb="file-uploader"] {
        background-color: #f8f9fa !important;
        border-radius: 6px !important;
        border: none !important;
    }
    
    [data-baseweb="file-uploader"] * {
        color: #111111 !important;
    }
    
    /* File uploader button */
    [data-testid="stFileUploader"] button {
        background-color: #0066cc !important;
        color: #ffffff !important;
        border: 1px solid #0088ff !important;
        border-radius: 4px !important;
    }
    
    /* Info boxes */
    .stInfo {
        background-color: #e8f4ff;
        border-left: 4px solid #0066cc;
    }
    
    /* Success boxes */
    .stSuccess {
        background-color: #e9f7ef;
        border-left: 4px solid #00cc00;
    }
    
    /* Warning boxes */
    .stWarning {
        background-color: #fff8e5;
        border-left: 4px solid #ffaa00;
    }
    
    /* Error boxes */
    .stError {
        background-color: #fdecea;
        border-left: 4px solid #cc0000;
    }
    
    /* Dividers */
    hr {
        border-color: #333333;
    }
    
    /* Radio buttons - make them more visible and user-friendly */
    .stRadio > label {
        color: #111111 !important;
        font-weight: 500 !important;
        font-size: 16px !important;
    }
    
    .stRadio > div > label {
        color: #111111 !important;
        padding: 8px 12px !important;
        border-radius: 4px !important;
        transition: background-color 0.2s ease !important;
    }
    
    .stRadio > div > label:hover {
        background-color: #f5f5f5 !important;
    }
    
    /* Radio button input styling - use default browser circle with accent color */
    .stRadio input[type="radio"] {
        accent-color: #0066cc !important;
    }
    
    /* Metrics */
    [data-testid="stMetricValue"] {
        color: #111111;
    }
    
    [data-testid="stMetricLabel"] {
        color: #666666;
    }
    
    /* Code blocks - make JSON readable with dark background and bright text */
    .stCodeBlock {
        background-color: #f5f5f5 !important;
        border: 1px solid #dddddd !important;
        border-radius: 4px !important;
    }
    
    /* Code block text - make it bright and readable */
    .stCodeBlock code,
    .stCodeBlock pre,
    .stCodeBlock pre code {
        background-color: #f5f5f5 !important;
        color: #111111 !important;
        font-family: 'Courier New', monospace !important;
    }
    
    /* Target Streamlit's code display - comprehensive targeting */
    [data-testid="stCodeBlock"],
    [data-testid="stCodeBlock"] > div,
    [data-testid="stCodeBlock"] pre {
        background-color: #f5f5f5 !important;
        color: #111111 !important;
    }
    
    [data-testid="stCodeBlock"] code,
    [data-testid="stCodeBlock"] pre,
    [data-testid="stCodeBlock"] pre code,
    [data-testid="stCodeBlock"] pre span {
        background-color: #f5f5f5 !important;
        color: #111111 !important;
    }
    
    /* Expander content with code blocks */
    .streamlit-expanderContent .stCodeBlock,
    .streamlit-expanderContent [data-testid="stCodeBlock"] {
        background-color: #f5f5f5 !important;
    }
    
    .streamlit-expanderContent .stCodeBlock code,
    .streamlit-expanderContent [data-testid="stCodeBlock"] code,
    .streamlit-expanderContent [data-testid="stCodeBlock"] pre {
        background-color: #f5f5f5 !important;
        color: #111111 !important;
    }
    
    /* Override any white backgrounds in code blocks */
    pre[class*="language"],
    code[class*="language"],
    pre,
    code {
        background-color: #f5f5f5 !important;
        color: #111111 !important;
    }
    
    /* Target any element inside code blocks */
    [data-testid="stCodeBlock"] * {
        color: #111111 !important;
    }
    
    /* Expanders */
    .streamlit-expanderHeader {
        background-color: #f5f5f5;
        color: #111111;
    }
    
    /* Make expander chevron arrows bright and visible */
    .streamlit-expanderHeader svg {
        color: #333333 !important;
        stroke: #333333 !important;
        fill: #333333 !important;
    }
    
    .streamlit-expanderHeader:hover svg {
        color: #0066cc !important;
        stroke: #0066cc !important;
        fill: #0066cc !important;
    }
    
    /* Expander header text */
    .streamlit-expanderHeader p {
        color: #111111 !important;
    }
    
    /* Expander content area */
    .streamlit-expanderContent {
        background-color: #ffffff;
        color: #111111;
    }
    
    /* Status text and processing messages - make them bright white */
    .stText {
        color: #111111 !important;
    }
    
    /* Progress bar text and status messages - comprehensive targeting */
    [data-testid="stText"],
    [data-testid="stText"] *,
    [data-testid="stText"] p,
    [data-testid="stText"] div,
    [data-testid="stText"] span {
        color: #111111 !important;
    }
    
    /* Spinner text - make it bright white */
    [data-testid="stSpinner"],
    [data-testid="stSpinner"] *,
    [data-testid="stSpinner"] p,
    [data-testid="stSpinner"] div,
    [data-testid="stSpinner"] span {
        color: #111111 !important;
    }
    
    /* All text in empty containers (status_text.text()) */
    [data-testid="stEmpty"] *,
    [data-testid="stEmpty"] p,
    [data-testid="stEmpty"] div,
    [data-testid="stEmpty"] span {
        color: #111111 !important;
    }
    
    /* Target all text elements more aggressively */
    .element-container p,
    .element-container div,
    .element-container span {
        color: #111111 !important;
    }
    
    /* Streamlit markdown and text containers */
    .stMarkdown p,
    .stMarkdown div,
    .stMarkdown span,
    [data-testid="stMarkdownContainer"] p,
    [data-testid="stMarkdownContainer"] div,
    [data-testid="stMarkdownContainer"] span {
        color: #111111 !important;
    }
    
    /* Override grey text colors specifically */
    p[style*="color"],
    div[style*="color"],
    span[style*="color"] {
        color: #111111 !important;
    }
    
    /* Target any element with grey/light colors */
    *[style*="color: rgb"],
    *[style*="color:rgba"] {
        color: #111111 !important;
    }
</style>
"""
