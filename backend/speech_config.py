"""Validated user-facing wording; structured branches remain in the callers."""
from backend.prompt_config import config_text, validate_config
from backend.textutil import halfwidth_alnum
from jsonschema import ValidationError

RECEIPTS_FIELDS = {'labels.seat.driver': set(),
 'labels.seat.front_passenger': set(),
 'labels.seat.rear_left': set(),
 'labels.seat.rear_right': set(),
 'labels.zone.all': set(),
 'labels.zone.driver': set(),
 'labels.zone.front_passenger': set(),
 'labels.zone.rear': set(),
 'labels.zone.front': set(),
 'labels.enabled': set(),
 'labels.disabled': set(),
 'labels.all': set(),
 'labels.left': set(),
 'labels.right': set(),
 'labels.front': set(),
 'labels.rear': set(),
 'labels.locked': set(),
 'labels.unlocked': set(),
 'labels.folded': set(),
 'labels.unfolded': set(),
 'set_climate_power.text': {'enabled'},
 'set_temperature.text': {'zone', 'temperature'},
 'adjust_temperature.uniform': {'zone', 'temperature'},
 'adjust_temperature.mixed': {'temperatures'},
 'adjust_temperature.item': {'zone', 'temperature'},
 'set_fan_speed.text': {'level'},
 'set_window_position.closed': set(),
 'set_window_position.open': set(),
 'set_window_position.half': set(),
 'set_window_position.partial': {'position'},
 'set_window_position.text': {'seat', 'detail'},
 'close_all_windows.text': set(),
 'set_window_child_lock.text': {'enabled'},
 'set_seat_heating.level': {'level', 'seat'},
 'set_seat_heating.off': {'seat'},
 'set_seat_ventilation.level': {'level', 'seat'},
 'set_seat_ventilation.off': {'seat'},
 'set_seat_massage.off': {'seat'},
 'set_seat_massage.mode': {'seat', 'mode'},
 'set_seat_massage.modes.relax': set(),
 'set_seat_massage.modes.lumbar': set(),
 'set_seat_massage.modes.wave': set(),
 'set_seat_massage.modes.off': set(),
 'set_seat_position.axes.fore_aft': set(),
 'set_seat_position.axes.height': set(),
 'set_seat_position.axes.backrest': set(),
 'set_seat_position.axes.lumbar': set(),
 'set_seat_position.degrees': {'value'},
 'set_seat_position.percent': {'value'},
 'set_seat_position.text': {'seat', 'target', 'axis'},
 'set_interior_light.text': {'zone', 'enabled'},
 'set_volume.text': {'volume'},
 'control_playback.play': set(),
 'control_playback.pause': set(),
 'control_playback.next': set(),
 'control_playback.previous': set(),
 'control_playback.stop': set(),
 'control_playback.default': set(),
 'set_navigation_destination.text': {'destination'},
 'set_mirror_heating.text': {'enabled'},
 'set_mirror_fold.text': {'folded'},
 'set_steering_heating.text': {'enabled'},
 'set_reading_light.text': {'seat', 'enabled'},
 'set_ambient_light.text': {'enabled'},
 'set_ambient_color.text': {'color'},
 'set_ambient_color.colors.white': set(),
 'set_ambient_color.colors.warm': set(),
 'set_ambient_color.colors.red': set(),
 'set_ambient_color.colors.blue': set(),
 'set_ambient_color.colors.green': set(),
 'set_ambient_color.colors.purple': set(),
 'set_ambient_brightness.text': {'brightness'},
 'set_child_lock.text': {'side', 'enabled'},
 'set_door_lock.text': {'door', 'locked'},
 'lock_all_doors.text': set(),
 'unlock_all_doors.text': set(),
 'set_tailgate_open.text': {'enabled'},
 'set_tailgate_height.text': {'height'},
 'set_wiper_mode.off': set(),
 'set_wiper_mode.text': {'mode'},
 'set_wiper_mode.modes.auto': set(),
 'set_wiper_mode.modes.intermittent': set(),
 'set_wiper_mode.modes.low': set(),
 'set_wiper_mode.modes.high': set(),
 'set_wiper_interval.text': {'seconds'},
 'set_rain_sensitivity.text': {'level'},
 'set_rear_wiper.text': {'enabled'},
 'wash_windshield.text': {'target'},
 'set_sunroof_position.text': {'position'},
 'set_sunshade_position.text': {'position'},
 'get_climate_state.text': set(),
 'adjust_fan_speed.text': set(),
 'set_airflow_mode.text': set(),
 'set_recirculation.text': set(),
 'set_auto_climate.text': set(),
 'set_front_defrost.text': set(),
 'set_rear_defrost.text': set(),
 'set_air_purifier.text': set(),
 'set_fragrance.text': set(),
 'set_humidity.text': set(),
 'set_temperature_unit.text': set(),
 'sync_climate_zones.text': set(),
 'set_rear_climate_power.text': set(),
 'get_air_quality.text': set(),
 'get_seat_state.text': set(),
 'adjust_seat_position.text': set(),
 'save_seat_memory.text': set(),
 'recall_seat_memory.text': set(),
 'set_steering_position.text': set(),
 'set_easy_entry.text': set(),
 'set_seat_occupancy.text': set(),
 'get_seat_memory.text': set(),
 'set_seat_cushion_length.text': set(),
 'set_seat_side_support.text': set(),
 'set_seat_headrest.text': set(),
 'get_window_state.text': set(),
 'adjust_window_position.text': set(),
 'open_all_windows.text': set(),
 'set_window_anti_pinch.text': set(),
 'set_rain_close_windows.text': set(),
 'get_mirror_state.text': set(),
 'set_mirror_angle.text': set(),
 'adjust_mirror_angle.text': set(),
 'set_reverse_mirror_tilt.text': set(),
 'save_mirror_memory.text': set(),
 'recall_mirror_memory.text': set(),
 'get_light_state.text': set(),
 'set_headlight_mode.text': set(),
 'set_high_beam.text': set(),
 'set_front_fog_lights.text': set(),
 'set_rear_fog_light.text': set(),
 'set_follow_me_home.text': set(),
 'set_hazard_lights.text': set(),
 'get_lock_state.text': set(),
 'set_auto_lock.text': set(),
 'set_walk_away_lock.text': set(),
 'set_charge_port_lock.text': set(),
 'get_tailgate_state.text': set(),
 'stop_tailgate.text': set(),
 'get_wiper_state.text': set(),
 'get_media_state.text': set(),
 'adjust_volume.text': set(),
 'set_mute.text': set(),
 'set_media_source.text': set(),
 'play_media.text': set(),
 'set_radio_frequency.text': set(),
 'set_audio_balance.text': set(),
 'set_equalizer.text': set(),
 'get_navigation_state.text': set(),
 'add_navigation_waypoint.text': set(),
 'remove_navigation_waypoint.text': set(),
 'set_navigation_preferences.text': set(),
 'cancel_navigation.text': set(),
 'set_home_destination.text': set(),
 'set_work_destination.text': set()}

validate_config('speech/receipts.toml', RECEIPTS_FIELDS)

def receipt_text(key, **variables):
    return config_text('speech/receipts.toml', key, **{name: str(value) for name, value in variables.items()})


PERSONA_FIELDS = {'professional.name': set(),
 'professional.tone': set(),
 'professional.sentence_length': set(),
 'professional.catchphrase_frequency': set(),
 'warm.name': set(),
 'warm.tone': set(),
 'warm.sentence_length': set(),
 'warm.catchphrase_frequency': set(),
 'playful.name': set(),
 'playful.tone': set(),
 'playful.sentence_length': set(),
 'playful.catchphrase_frequency': set(),
 'success.replacements.current': set(),
 'success.replacements.adjust': set(),
 'success.replacements.window_position': set(),
 'success.replacements.window_adjust': set(),
 'success.replacements.enabled': set(),
 'success.replacements.enable': set(),
 'success.replacements.disabled': set(),
 'success.replacements.disable': set(),
 'success.replacements.warm_adjust': set(),
 'success.replacements.open_half': set(),
 'success.replacements.warm_half': set(),
 'success.warm_particle': set(),
 'success.playful_particle': set(),
 'success.retained_particle': set(),
 'delegated.text': {'start', 'goal'},
 'delegated.professional': set(),
 'delegated.warm': set(),
 'delegated.playful': set(),
 'clarify.remove_ui_hint': set(),
 'clarify.warm_particle': set(),
 'clarify.playful_particle': set(),
 'clarify.existing_particles': set(),
 'refused.professional': set(),
 'refused.warm': set(),
 'refused.playful': set(),
 'error.professional': set(),
 'error.warm': set(),
 'error.playful': set(),
 'cancel.professional': set(),
 'cancel.warm': set(),
 'cancel.playful': set(),
 'updated.professional': set(),
 'updated.warm': set(),
 'updated.playful': set()}

validate_config('speech/persona.toml', PERSONA_FIELDS)

def persona_text(key, **variables):
    return config_text('speech/persona.toml', key, **{name: str(value) for name, value in variables.items()})


FEEDBACK_FIELDS = {'dispatch.grouped': set(),
 'dispatch.text': {'action'},
 'dispatch.seat.driver': set(),
 'dispatch.seat.front_passenger': set(),
 'dispatch.seat.rear_left': set(),
 'dispatch.seat.rear_right': set(),
 'dispatch.seat.all': set(),
 'dispatch.set_window_position.closed': {'seat'},
 'dispatch.set_window_position.open': {'seat'},
 'dispatch.set_window_position.partial': {'position', 'seat'},
 'dispatch.close_all_windows': set(),
 'dispatch.set_navigation_destination': {'destination'},
 'dispatch.set_temperature': {'temperature'},
 'dispatch.fallback': set(),
 'task.phases.search_web': set(),
 'task.phases.lookup_web': set(),
 'task.phases.extract_web': set(),
 'task.phases.get_weather_forecast': set(),
 'task.phases.load_skill': set(),
 'task.phases.suggest_vehicle_action': set(),
 'task.phases.finish_task': set(),
 'task.phases.model': set(),
 'task.errors.deadline': set(),
 'task.errors.step_budget': set(),
 'task.errors.clarification_timeout': set(),
 'task.errors.execution_failed': set(),
 'task.errors.finalization_failed': set(),
 'task.sources_retained': set(),
 'task.needs_condition': set(),
 'task.cancelled': set(),
 'task.cancelling': set(),
 'task.queued': set(),
 'task.read_sources': {'count'},
 'task.partial_prefix': set(),
 'task.updated': set(),
 'task.suggestions': set(),
 'task.not_found': set(),
 'task.select': set(),
 'task.select_question': set(),
 'places.two': set(),
 'places.single': {'name'},
 'places.few': {'count', 'names'},
 'places.many': {'count', 'names'},
 'places.navigation_started': {'name'},
 'errors.parameters': set(),
 'errors.unavailable_tool': set(),
 'errors.field.volume': set(),
 'errors.field.temperature': set(),
 'errors.field.level': set(),
 'errors.field.position': set(),
 'errors.field.default': set(),
 'errors.temperature_unit': set(),
 'errors.range': {'label', 'minimum', 'unit', 'maximum'},
 'errors.instruction_format': set(),
 'errors.model_connection_pending': set(),
 'errors.model_connection': set(),
 'errors.no_function': set(),
 'errors.planner_unverified': set(),
 'errors.planner_incomplete': set(),
 'errors.planner_group_rejected': set(),
 'errors.destination_unconfirmed': set(),
 'errors.area_unconfirmed': set(),
 'errors.receipt_timeout': set(),
 'errors.vehicle_rejected': set(),
 'errors.followup_not_executed': set(),
 'errors.vehicle_failure': {'reasons'},
 'errors.action_not_executed': set(),
 'errors.plan_not_executed': set(),
 'errors.executor_rejected': set(),
 'vehicle.kept_settings': set(),
 'errors.audio_receiver_slow': set(),
 'errors.lookup_timeout': set(),
 'errors.lookup_failed': set(),
 'errors.vehicle_unclear': set(),
 'errors.dependency_no_receipt': set(),
 'errors.keep_changed': set(),
 'errors.keep_write': set(),
 'errors.keep_mutation': set(),
 'errors.dependency_failed': set(),
 'errors.keep_unconfirmed': set(),
 'errors.no_receipt': set(),
 'maps.area_not_found': {'near'},
 'maps.relevance_notice': set(),
 'maps.no_places': set(),
 'maps.city_clarification': set(),
 'maps.forecast_interval': set(),
 'maps.unknown_condition': set(),
 'maps.fallback_notice': set(),
 'weather.city_clarification': set(),
 'weather.city_not_found': set(),
 'manual.images_pending': set(),
 'manual.provider': set(),
 'manual.page_caption': {'page'},
 'manual.images_unavailable': set(),
 'format.labels.summary': set(),
 'format.labels.answer': set(),
 'format.labels.spoken_summary': set(),
 'format.labels.reason': set(),
 'format.labels.error': set(),
 'format.labels.temperature': set(),
 'format.labels.status': set(),
 'format.json_item': {'label', 'value'},
 'format.true': set(),
 'format.false': set(),
 'format.none': set(),
 'format.url_fallback': set(),
 'simulator.stale_command': set(),
 'simulator.keep_changed': set(),
 'simulator.keep_mutation': set(),
 'maps.conditions.code_0': set(),
 'maps.conditions.code_1': set(),
 'maps.conditions.code_2': set(),
 'maps.conditions.code_3': set(),
 'maps.conditions.code_45': set(),
 'maps.conditions.code_48': set(),
 'maps.conditions.code_51': set(),
 'maps.conditions.code_53': set(),
 'maps.conditions.code_55': set(),
 'maps.conditions.code_56': set(),
 'maps.conditions.code_57': set(),
 'maps.conditions.code_61': set(),
 'maps.conditions.code_63': set(),
 'maps.conditions.code_65': set(),
 'maps.conditions.code_66': set(),
 'maps.conditions.code_67': set(),
 'maps.conditions.code_71': set(),
 'maps.conditions.code_73': set(),
 'maps.conditions.code_75': set(),
 'maps.conditions.code_77': set(),
 'maps.conditions.code_80': set(),
 'maps.conditions.code_81': set(),
 'maps.conditions.code_82': set(),
 'maps.conditions.code_85': set(),
 'maps.conditions.code_86': set(),
 'maps.conditions.code_95': set(),
 'maps.conditions.code_96': set(),
 'maps.conditions.code_99': set(),
 'web_search.notice': set(),
 'task.db.step': {'step'},
 'task.db.queued': set(),
 'task.db.running': set(),
 'task.db.cancel_pending': set(),
 'task.db.update_pending': set(),
 'task.db.clarification_label': set()}

validate_config('speech/feedback.toml', FEEDBACK_FIELDS)

def feedback_text(key, **variables):
    return config_text('speech/feedback.toml', key, **{name: str(value) for name, value in variables.items()})


def count_words(n):
    from backend.speech_text import spoken_number
    return feedback_text('places.two') if n == 2 else spoken_number(n)


def places_sentence(names):
    """Ordinals in speech and result cards refer to the same actual result set."""
    names = [halfwidth_alnum(name) for name in names]
    if len(names) == 1:
        return feedback_text('places.single', name=names[0])
    if len(names) <= 3:
        return feedback_text('places.few', count=count_words(len(names)), names='、'.join(names))
    return feedback_text('places.many', count=count_words(len(names)), names='、'.join(names[:3]))


def receipt_detail(name, args, result):
    """Select wording from verified structured receipts, never from user text."""
    from backend.speech_text import spoken_number
    seat = {key: receipt_text('labels.seat.' + key) for key in ('driver', 'front_passenger', 'rear_left', 'rear_right')}
    zone = {key: receipt_text('labels.zone.' + key) for key in ('all', 'driver', 'front_passenger', 'rear', 'front')}
    enabled = receipt_text('labels.enabled' if args.get('enabled') else 'labels.disabled')
    if name == 'set_climate_power':
        return receipt_text(name + '.text', enabled=enabled)
    if name == 'set_temperature':
        return receipt_text(name + '.text', zone='' if args['zone'] == 'all' else zone.get(args['zone'], args['zone']),
                            temperature=spoken_number(args['temperature']))
    if name == 'adjust_temperature':
        temperatures = result['value']
        selected = temperatures if args['zone'] == 'all' else {args['zone']: temperatures[args['zone']]}
        if len(set(selected.values())) == 1:
            return receipt_text(name + '.uniform', zone=zone['all'] if args['zone'] == 'all' else zone[args['zone']],
                                temperature=spoken_number(next(iter(selected.values()))))
        values = '、'.join(receipt_text(name + '.item', zone=zone[key], temperature=spoken_number(value)) for key, value in selected.items())
        return receipt_text(name + '.mixed', temperatures=values)
    if name == 'set_fan_speed':
        return receipt_text(name + '.text', level=args['level'])
    if name == 'set_window_position':
        position = args['position']
        detail = receipt_text(name + '.closed') if position == 0 else receipt_text(name + '.open') if position == 100 else receipt_text(name + '.half') if position == 50 else receipt_text(name + '.partial', position=f'{position:g}')
        return receipt_text(name + '.text', seat=seat.get(args['window'], args['window']), detail=detail)
    if name == 'close_all_windows':
        return receipt_text(name + '.text')
    if name == 'set_window_child_lock':
        return receipt_text(name + '.text', enabled=receipt_text('labels.enabled' if result['value'] else 'labels.disabled'))
    if name in {'set_seat_heating', 'set_seat_ventilation'}:
        values = {'seat': seat.get(args['seat'], args['seat'])}
        if args['level']:
            return receipt_text(name + '.level', **values, level=args['level'])
        return receipt_text(name + '.off', **values)
    if name == 'set_seat_massage':
        mode = receipt_text(name + '.modes.' + args['mode'])
        values = {'seat': seat.get(args['seat'], args['seat'])}
        return receipt_text(name + '.off', **values) if args['mode'] == 'off' else receipt_text(name + '.mode', **values, mode=mode)
    if name == 'set_seat_position':
        axis = receipt_text(name + '.axes.' + args['axis'])
        target = receipt_text(name + ('.degrees' if args['axis'] == 'backrest' else '.percent'), value=spoken_number(result['value']))
        return receipt_text(name + '.text', seat=seat.get(args['seat'], args['seat']), axis=axis, target=target)
    if name == 'set_interior_light':
        return receipt_text(name + '.text', zone=zone.get(args.get('zone'), ''), enabled=enabled)
    if name == 'set_volume':
        return receipt_text(name + '.text', volume=args['volume'])
    if name == 'control_playback':
        action = args.get('action')
        return receipt_text(name + '.' + (action if action in {'play', 'pause', 'next', 'previous', 'stop'} else 'default'))
    if name == 'set_navigation_destination':
        return receipt_text(name + '.text', destination=args['destination'])
    if name in {'set_mirror_heating', 'set_steering_heating', 'set_ambient_light', 'set_rear_wiper'}:
        return receipt_text(name + '.text', enabled=enabled)
    if name == 'set_mirror_fold':
        return receipt_text(name + '.text', folded=receipt_text('labels.folded' if result['value'] else 'labels.unfolded'))
    if name == 'set_reading_light':
        return receipt_text(name + '.text', seat=seat.get(args['seat'], args['seat']), enabled=enabled)
    if name == 'set_ambient_color':
        return receipt_text(name + '.text', color=receipt_text(name + '.colors.' + args['color']))
    if name == 'set_ambient_brightness':
        return receipt_text(name + '.text', brightness=args['brightness'])
    if name == 'set_child_lock':
        return receipt_text(name + '.text', side=receipt_text('labels.left' if args['side'] == 'left' else 'labels.right'), enabled=enabled)
    if name == 'set_door_lock':
        return receipt_text(name + '.text', door=receipt_text('labels.all') if args['door'] == 'all' else seat.get(args['door'], args['door']),
                            locked=receipt_text('labels.locked' if args['locked'] else 'labels.unlocked'))
    if name in {'lock_all_doors', 'unlock_all_doors'}:
        return receipt_text(name + '.text')
    if name == 'set_tailgate_open':
        return receipt_text(name + '.text', enabled=receipt_text('labels.enabled' if args['open'] else 'labels.disabled'))
    if name == 'set_tailgate_height':
        return receipt_text(name + '.text', height=args['height'])
    if name == 'set_wiper_mode':
        return receipt_text(name + '.off') if args['mode'] == 'off' else receipt_text(name + '.text', mode=receipt_text(name + '.modes.' + args['mode']))
    if name == 'set_wiper_interval':
        return receipt_text(name + '.text', seconds=args['seconds'])
    if name == 'set_rain_sensitivity':
        return receipt_text(name + '.text', level=spoken_number(result['value']))
    if name == 'wash_windshield':
        return receipt_text(name + '.text', target=receipt_text('labels.front' if args['target'] == 'front' else 'labels.rear'))
    if name in {'set_sunroof_position', 'set_sunshade_position'}:
        return receipt_text(name + '.text', position=args['position'])
    return receipt_text(name + '.text')


def spoken_reason(reason):
    """Keep existing compatibility with physical refusal and schema diagnostics."""
    text = str(reason)
    if text.startswith('参数校验失败'):
        return feedback_text('errors.parameters')
    if any(marker in text for marker in ('load_skill', 'skill_id', '未知工具', '未知函数')):
        return feedback_text('errors.unavailable_tool')
    return text


def validation_feedback(exc, name):
    from backend.speech_text import spoken_number
    from backend.catalog import TOOLS
    if isinstance(exc, ValidationError):
        field_name = next(iter(exc.path), None)
        spec = TOOLS[name].parameters.get('properties', {}).get(field_name, {}) if name in TOOLS else {}
        if exc.validator in {'minimum', 'maximum'} and 'minimum' in spec and 'maximum' in spec:
            field = field_name if field_name in {'volume', 'temperature', 'level', 'position'} else 'default'
            return feedback_text('errors.range', label=feedback_text('errors.field.' + field), minimum=spoken_number(spec['minimum']),
                               maximum=spoken_number(spec['maximum']), unit=feedback_text('errors.temperature_unit') if field_name == 'temperature' else '')
        return feedback_text('errors.instruction_format')
    return spoken_reason(f'参数校验失败：{str(exc).splitlines()[0][:180]}')
