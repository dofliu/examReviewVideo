import cairosvg
import os

svg_code = """<svg viewBox="0 0 400 300" xmlns="http://www.w3.org/2000/svg"><defs><marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#FFD96B" /></marker></defs><line x1="50" y1="150" x2="350" y2="150" stroke="#E8E6D8" stroke-width="1" stroke-dasharray="5,5" /><line x1="100" y1="50" x2="100" y2="250" stroke="#E8E6D8" stroke-width="1" stroke-dasharray="5,5" /><text x="340" y="145" fill="#E8E6D8" font-size="14">x</text><text x="105" y="60" fill="#E8E6D8" font-size="14">y</text><line x1="100" y1="150" x2="300" y2="150" stroke="#FFD96B" stroke-width="4" marker-end="url(#arrow)" /><text x="250" y="140" fill="#FFD96B" font-size="16" font-weight="bold">R = 10 kN</text><line x1="100" y1="150" x2="220" y2="80" stroke="#FFD96B" stroke-width="3" marker-end="url(#arrow)" /><text x="225" y="85" fill="#FFD96B" font-size="16">FA</text><path d="M 130 150 A 30 30 0 0 0 126 135" fill="none" stroke="#E8E6D8" stroke-width="1" /><text x="135" y="140" fill="#E8E6D8" font-size="12">30°</text><line x1="100" y1="150" x2="250" y2="190" stroke="#FFD96B" stroke-width="3" marker-end="url(#arrow)" /><text x="255" y="205" fill="#FFD96B" font-size="16">FB</text><path d="M 130 150 A 30 30 0 0 1 129 158" fill="none" stroke="#E8E6D8" stroke-width="1" /><text x="135" y="165" fill="#E8E6D8" font-size="12">15°</text></svg>"""

try:
    cairosvg.svg2png(bytestring=svg_code.encode("utf-8"), write_to="test_svg.png")
    print("Success")
except Exception as e:
    print(f"Error: {e}")
