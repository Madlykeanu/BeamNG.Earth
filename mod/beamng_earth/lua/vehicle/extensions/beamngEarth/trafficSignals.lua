-- Waypoint driving does not run the stock traffic mode's junction actions.
-- Read its public signal data and return a braking ceiling for the bound path.
local M={STOP_M=3,DECEL=3,REACTION_S=.25}
local function finite(x)return type(x)=='number'and x==x and math.abs(x)<math.huge end
local function position(p)return p and finite(p.x)and finite(p.y)and finite(p.z)end

function M.limit(c,md,signals,speed)
  if not c or not c.points or not finite(c.radius)or not md or not md.positions or not md.graph
    or type(signals)~='table'or not finite(speed)then return nil end
  speed=math.max(0,speed)
  local seen,best={},nil
  for _,point in ipairs(c.points)do
    local a,b=point.from,point.to
    local key=a and b and(tostring(a)..'/'..tostring(b))
    if key and not seen[key]then
      seen[key]=true
      local edge=md.graph[a]and md.graph[a][b]
      local entries=signals[a]and signals[a][b]
      local signal=entries and entries[1] -- the same approach signal used by stock traffic
      local pa,pb=md.positions[a],md.positions[b]
      if edge and(not edge.oneWay or edge.inNode==nil or edge.inNode==a)
        and signal and(signal.action==1 or signal.action==2)and position(signal.pos)
        and position(pa)and position(pb)and position(point)and finite(point.along)then
        local dx,dy,dz=pb.x-pa.x,pb.y-pa.y,pb.z-pa.z
        local ll=dx*dx+dy*dy+dz*dz
        if ll>.01 then
          local q=signal.pos
          local t=((q.x-pa.x)*dx+(q.y-pa.y)*dy+(q.z-pa.z)*dz)/ll
          local length=math.sqrt(ll)
          local x,y,z=pa.x+t*dx,pa.y+t*dy,pa.z+t*dz
          local margin=math.max(4,c.radius+2)
          -- A roadside head can be offset from its edge. Another road level or
          -- a stale signal outside this edge is not a stop on the current path.
          if t>=-margin/length and t<=1+margin/length and math.abs(q.z-z)<=2
            and(q.x-x)^2+(q.y-y)^2<=margin*margin then
            local distance=point.along+((q.x-point.x)*dx+(q.y-point.y)*dy+(q.z-point.z)*dz)/length
            local stop=M.STOP_M+speed*M.REACTION_S
            local canStop=distance>=stop+speed*speed/(2*M.DECEL)
            -- Clear a yellow that is already too close to stop for. Never aim
            -- back at a stop line already passed, including on a graph handoff.
            if distance>=-.25 and(signal.action==2 or canStop)then
              local cap=math.sqrt(2*M.DECEL*math.max(0,distance-stop))
              if not best or cap<best.cap then
                best={cap=cap,distance=distance,instance=signal.instance,action=signal.action,
                  phase='signal',reason=signal.action==2 and'red light'or'yellow light'}
              end
            end
          end
        end
      end
    end
  end
  return best
end
return M
