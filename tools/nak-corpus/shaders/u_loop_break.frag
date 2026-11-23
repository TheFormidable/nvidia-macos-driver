#version 450
layout(location=0) out vec4 o;
layout(push_constant) uniform P { int n; } p;
void main(){ vec4 a=vec4(gl_FragCoord.x); for(int i=0;i<p.n;i++){ if(a.x>float(i)) { a+=vec4(1); if (a.y>9.0) break; } else a*=0.5; } o=a; }
