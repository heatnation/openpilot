from cereal import log
from common.numpy_fast import clip, interp
from selfdrive.controls.lib.pid import PIController
from common.travis_checker import travis
from selfdrive.car.toyota.values import CAR as CAR_TOYOTA
from common.op_params import opParams
from selfdrive.controls.lib.dynamic_lane_speed import DynamicLaneSpeed

LongCtrlState = log.ControlsState.LongControlState

STOPPING_EGO_SPEED = 0.5
MIN_CAN_SPEED = 0.3  # TODO: parametrize this in car interface
STOPPING_TARGET_SPEED = MIN_CAN_SPEED + 0.01
STARTING_TARGET_SPEED = 0.5
BRAKE_THRESHOLD_TO_PID = 0.2

STOPPING_BRAKE_RATE = 0.2  # brake_travel/s while trying to stop
STARTING_BRAKE_RATE = 0.8  # brake_travel/s while releasing on restart
BRAKE_STOPPING_TARGET = 0.75  # apply at least this amount of brake to maintain the vehicle stationary

_MAX_SPEED_ERROR_BP = [0., 30.]  # speed breakpoints
_MAX_SPEED_ERROR_V = [1.5, .8]  # max positive v_pid error VS actual speed; this avoids controls windup due to slow pedal resp

RATE = 100.0


def long_control_state_trans(active, long_control_state, v_ego, v_target, v_pid,
                             output_gb, brake_pressed, cruise_standstill, stop):
  """Update longitudinal control state machine"""
  stopping_condition = stop or (v_ego < 2.0 and cruise_standstill) or \
                       (v_ego < STOPPING_EGO_SPEED and \
                        ((v_pid < STOPPING_TARGET_SPEED and v_target < STOPPING_TARGET_SPEED) or
                        brake_pressed))

  starting_condition = v_target > STARTING_TARGET_SPEED and not cruise_standstill

  if not active:
    long_control_state = LongCtrlState.off

  else:
    if long_control_state == LongCtrlState.off:
      if active:
        long_control_state = LongCtrlState.pid

    elif long_control_state == LongCtrlState.pid:
      if stopping_condition:
        long_control_state = LongCtrlState.stopping

    elif long_control_state == LongCtrlState.stopping:
      if starting_condition:
        long_control_state = LongCtrlState.starting

    elif long_control_state == LongCtrlState.starting:
      if stopping_condition:
        long_control_state = LongCtrlState.stopping
      elif output_gb >= -BRAKE_THRESHOLD_TO_PID:
        long_control_state = LongCtrlState.pid

  return long_control_state


class LongControl():
  def __init__(self, CP, compute_gb, candidate):
    self.long_control_state = LongCtrlState.off  # initialized to off
    self.pid = PIController((CP.longitudinalTuning.kpBP, CP.longitudinalTuning.kpV),
                            (CP.longitudinalTuning.kiBP, CP.longitudinalTuning.kiV),
                            rate=RATE,
                            sat_limit=0.8,
                            convert=compute_gb)
    self.v_pid = 0.0
    self.lastdecelForTurn = False
    #self.had_lead = False
    self.last_output_gb = 0.0

    self.op_params = opParams()
    self.candidate = candidate
    self.toyota_candidates = [attr for attr in dir(CAR_TOYOTA) if not attr.startswith("__")]

    self.gas_pressed = False
    self.lead_data = {'v_rel': None, 'a_lead': None, 'x_lead': None, 'status': False}
    self.track_data = []
    self.mpc_TR = 1.8
    self.v_ego = 0.0
    self.dynamic_lane_speed = DynamicLaneSpeed()
    self.last_v_target = 0


  def reset(self, v_pid):
    """Reset PID controller and change setpoint"""
    self.pid.reset()
    self.v_pid = v_pid

    def dynamic_gas(self, CP):
    x, y = [], []
    if CP.enableGasInterceptor:  # todo: make different profiles for different vehicles
      if self.candidate == CAR_TOYOTA.COROLLA:
        x = [0.0, 1.4082, 2.80311, 4.22661, 5.38271, 6.16561, 7.24781, 8.28308, 10.24465, 12.96402, 15.42303, 18.11903, 20.11703, 24.46614, 29.05805, 32.71015, 35.76326]
        y = [0.2, 0.20443, 0.21592, 0.23334, 0.25734, 0.27916, 0.3229, 0.35, 0.368, 0.377, 0.389, 0.399, 0.411, 0.45, 0.504, 0.558, 0.617]
      elif self.candidate == CAR_TOYOTA.RAV4:
        x = [0.0, 1.4082, 2.80311, 4.22661, 5.38271, 6.16561, 7.24781, 8.28308, 10.24465, 12.96402, 15.42303, 18.11903, 20.11703, 24.46614, 29.05805, 32.71015, 35.76326]
        y = [0.234, 0.237, 0.246, 0.26, 0.279, 0.297, 0.332, 0.354, 0.368, 0.377, 0.389, 0.399, 0.411, 0.45, 0.504, 0.558, 0.617]

    if not x:
      # x, y = CP.gasMaxBP, CP.gasMaxV  # if unsupported car, use stock.
      return interp(self.v_ego, CP.gasMaxBP, CP.gasMaxV)

    gas = interp(self.v_ego, x, y)

    if self.lead_data['status']:  # if lead
      if self.v_ego <= 8.9408:  # if under 20 mph
        x = [0.0, 0.24588812499999999, 0.432818589, 0.593044697, 0.730381365, 1.050833588, 1.3965, 1.714627481]  # relative velocity mod
        y = [gas * 0.9901, gas * 0.905, gas * 0.8045, gas * 0.625, gas * 0.431, gas * 0.2083, gas * .0667, 0]
        gas_mod = -interp(self.lead_data['v_rel'], x, y)

        x = [0.44704, 1.78816]  # lead accel mod
        y = [0.0, gas_mod * .4]  # maximum we can reduce gas_mod is 40 percent of it
        gas_mod -= interp(self.lead_data['a_lead'], x, y)  # reduce the reduction of the above mod (the max this will ouput is the original gas value, it never increases it)

        # x = [TR * 0.5, TR, TR * 1.5]  # as lead gets further from car, lessen gas mod  # todo: this
        # y = [gas_mod * 1.5, gas_mod, gas_mod * 0.5]
        # gas_mod += (interp(current_TR, x, y))
        new_gas = gas + gas_mod

        x = [1.78816, 6.0, 8.9408]  # slowly ramp mods down as we approach 20 mph
        y = [new_gas, (new_gas * 0.8 + gas * 0.2), gas]
        gas = interp(self.v_ego, x, y)
      else:
        x = [-1.78816, -0.89408, 0, 1.78816, 2.68224]  # relative velocity mod
        y = [-gas * 0.35, -gas * 0.25, -gas * 0.075, gas * 0.1575, gas * 0.2025]
        gas_mod = interp(self.lead_data['v_rel'], x, y)

        current_TR = self.lead_data['x_lead'] / self.v_ego
        x = [self.mpc_TR - 0.22, self.mpc_TR, self.mpc_TR + 0.2, self.mpc_TR + 0.4]
        y = [-gas_mod * 0.36, 0.0, gas_mod * 0.15, gas_mod * 0.4]
        gas_mod -= interp(current_TR, x, y)

        gas += gas_mod

    return clip(gas, 0.0, 1.0)

  def handle_passable(self, passable):
    self.gas_pressed = passable['gas_pressed']
    self.lead_data['v_rel'] = passable['lead_one'].vRel
    self.lead_data['a_lead'] = passable['lead_one'].aLeadK
    self.lead_data['x_lead'] = passable['lead_one'].dRel
    self.lead_data['status'] = passable['has_lead']  # this fixes radarstate always reporting a lead, thanks to arne
    self.mpc_TR = passable['mpc_TR']
    self.track_data = []
    for track in passable['live_tracks']:
      self.track_data.append({'v_lead': self.v_ego + track.vRel, 'y_rel': track.yRel, 'x_lead': track.dRel})

  def update(self, active, v_ego, brake_pressed, standstill, cruise_standstill, v_cruise, v_target, v_target_future, a_target, CP, passable):
    """Update longitudinal control. This updates the state machine and runs a PID loop"""
    self.last_v_target = v_target
    self.v_ego = v_ego
    # Actuation limits
     if not travis:
      self.handle_passable(passable)
      gas_max = self.dynamic_gas(CP)
      v_target, v_target_future, a_target = self.dynamic_lane_speed.update(v_target, v_target_future, v_cruise, a_target, self.v_ego, self.track_data, self.lead_data)
    else:
      gas_max = interp(v_ego, CP.gasMaxBP, CP.gasMaxV)
    brake_max = interp(v_ego, CP.brakeMaxBP, CP.brakeMaxV)

    # Update state machine
    output_gb = self.last_output_gb
    if hasLead:
      stop = True if dRel < 4.0 else False
    else:
      stop = False
    self.long_control_state = long_control_state_trans(active, self.long_control_state, v_ego,
                                                       v_target_future, self.v_pid, output_gb,
                                                       brake_pressed, cruise_standstill, stop)

    v_ego_pid = max(v_ego, MIN_CAN_SPEED)  # Without this we get jumps, CAN bus reports 0 when speed < 0.3

    if self.long_control_state == LongCtrlState.off or (self.gas_pressed and not travis):
      self.v_pid = v_ego_pid
      self.pid.reset()
      output_gb = 0.

    # tracking objects and driving
    elif self.long_control_state == LongCtrlState.pid:
      self.v_pid = v_target
      self.pid.pos_limit = gas_max
      self.pid.neg_limit = - brake_max

      # Toyota starts braking more when it thinks you want to stop
      # Freeze the integrator so we don't accelerate to compensate, and don't allow positive acceleration
      prevent_overshoot = not CP.stoppingControl and v_ego < 1.5 and v_target_future < 0.7
      deadzone = interp(v_ego_pid, CP.longitudinalTuning.deadzoneBP, CP.longitudinalTuning.deadzoneV)
      #if not self.had_lead and has_lead:
      #  if enableGasInterceptor:
      #    self.pid._k_p = ([0., 5., 35.], [1.2, 0.8, 0.5])
      #    self.pid._k_i = ([0., 35.], [0.18, 0.12])
      #  else:
      #    self.pid._k_p = ([0., 5., 35.], [3.6, 2.4, 1.5])
      #    self.pid._k_i = ([0., 35.], [0.54, 0.36])
      #elif self.had_lead and not has_lead:
      #  self.pid._k_p = (CP.longitudinalTuning.kpBP, CP.longitudinalTuning.kpV)
      #  self.pid._k_i = (CP.longitudinalTuning.kiBP, CP.longitudinalTuning.kiV)
      #self.had_lead = has_lead
      if longitudinalPlanSource == 'cruise':
        if decelForTurn and not self.lastdecelForTurn:
          self.lastdecelForTurn = True
          self.pid._k_p = (CP.longitudinalTuning.kpBP, [x * 0 for x in CP.longitudinalTuning.kpV])
          self.pid._k_i = (CP.longitudinalTuning.kiBP, [x * 0 for x in CP.longitudinalTuning.kiV])
          self.pid.i = 0.0
          self.pid.k_f=1.0
        if self.lastdecelForTurn and not decelForTurn:
          self.lastdecelForTurn = False
          self.pid._k_p = (CP.longitudinalTuning.kpBP, CP.longitudinalTuning.kpV)
          self.pid._k_i = (CP.longitudinalTuning.kiBP, CP.longitudinalTuning.kiV)
          self.pid.k_f=1.0
      else:
        self.lastdecelForTurn = False
        self.pid._k_p = (CP.longitudinalTuning.kpBP, [x * 1 for x in CP.longitudinalTuning.kpV])
        self.pid._k_i = (CP.longitudinalTuning.kiBP, [x * 1 for x in CP.longitudinalTuning.kiV])
        self.pid.k_f=1.0

      output_gb = self.pid.update(self.v_pid, v_ego_pid, speed=v_ego_pid, deadzone=deadzone, feedforward=a_target, freeze_integrator=prevent_overshoot)

      if prevent_overshoot:
        output_gb = min(output_gb, 0.0)

    # Intention is to stop, switch to a different brake control until we stop
    elif self.long_control_state == LongCtrlState.stopping:
      # Keep applying brakes until the car is stopped
      factor = 1
      if hasLead:
        factor = interp(dRel,[2.0,3.0,4.0,5.0,6.0,7.0,8.0,9.0], [10.0,5.0,2.0,1.0,0.5,0.1,0.0,-0.1])
      if not standstill or output_gb > -BRAKE_STOPPING_TARGET:
        output_gb -= STOPPING_BRAKE_RATE / RATE * factor
      output_gb = clip(output_gb, -brake_max, gas_max)

      self.v_pid = v_ego
      self.pid.reset()

    # Intention is to move again, release brake fast before handing control to PID
    elif self.long_control_state == LongCtrlState.starting:
      factor = 1
      if hasLead:
        factor = interp(dRel,[0.0,2.0,4.0,6.0], [0.0,0.5,1.0,2.0])
      if output_gb < -0.2:
        output_gb += STARTING_BRAKE_RATE / RATE * factor
      self.v_pid = v_ego
      self.pid.reset()

    self.last_output_gb = output_gb
    final_gas = clip(output_gb, 0., gas_max)
    final_brake = -clip(output_gb, -brake_max, 0.)

    return final_gas, final_brake
