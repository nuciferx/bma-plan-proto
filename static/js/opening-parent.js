// Opening parent geometry helpers — extracted from ui.html
// depends on ptInPoly (defined in inline script, resolved at call time)
function openingProbePoints(op){const pts=op?.pts||[];if(!pts.length)return[];const cx=pts.reduce((s,p)=>s+p.x,0)/pts.length,cy=pts.reduce((s,p)=>s+p.y,0)/pts.length;return[...pts,{x:cx,y:cy}];}
function openingInsidePoly(op,poly){if(!op?.closed||!poly?.closed||(op.pts||[]).length<3||(poly.pts||[]).length<3)return false;return openingProbePoints(op).every(p=>ptInPoly(p.x,p.y,poly.pts));}
function openingParentCandidates(op,polys){return(polys||[]).filter(p=>openingInsidePoly(op,p));}
function linkOpeningParent(op,polys){
  if(op.parentManual&&op.parentId){op.parentStatus="linked";op.parentCandidateIds=[];return polys.find(p=>String(p.id)===String(op.parentId))||null;}
  const existing=op.parentId?polys.find(p=>String(p.id)===String(op.parentId)):null;
  if(existing&&openingInsidePoly(op,existing)){op.parentStatus="linked";op.parentCandidateIds=[existing.id];return existing;}
  const candidates=openingParentCandidates(op,polys);
  op.parentCandidateIds=candidates.map(p=>p.id);
  if(candidates.length===1){op.parentId=candidates[0].id;op.parentStatus="linked";return candidates[0];}
  delete op.parentId;op.parentStatus=candidates.length>1?"ambiguous":"unlinked";return null;
}
function linkOpeningsInStore(store){const polys=(store?.polys||[]).filter(p=>p.closed);for(const op of(store?.openings||[])){if(op.closed)linkOpeningParent(op,polys);}}
