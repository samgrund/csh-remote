project('csh_remote', 'c',
	subproject_dir: 'lib',
	meson_version: '>= 1.6',
	version: '0.1.0',
	default_options: [
		'buildtype=debug',
		'optimization=2',
		'warning_level=3',
		'werror=true',
		'c_std=gnu18',
		'b_lto=false',       # LTO may discard the "slash" section symbols
		'b_lundef=false',    # symbols (slash, param, vmem, csp API) are
		                     # provided by the loading csh process
	])

add_project_arguments(['-Wstrict-prototypes', '-Wmissing-prototypes',
	               '-Wmissing-declarations', '-Wcast-align', '-Wpointer-arith',
	               '-Wshadow', '-Wno-unused-parameter'], language: 'c')

# Provides the default libmain() (which registers the slash commands defined
# below via the "slash" section, then calls our apm_init()), apm_init_version,
# and the APM headers.  Submodules are pinned to the same commits as the csh
# checkout this APM is meant to be loaded into.
deps = []
deps += dependency('apm_csh', fallback: ['apm_csh', 'apm_csh_dep'], required: true).as_link_whole()

csh_remote_lib = shared_library('csh_remote',
	['src/main_remote.c'],
	dependencies: deps,
	link_args: ['-Wl,-Map=csh_remote.map', '-pthread'],
	install: true,
)
