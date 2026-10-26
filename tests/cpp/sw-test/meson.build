project('sw-test', ['cpp'],
        default_options: ['warning_level=1', 'werror=true', 'cpp_std=c++20',
                          'b_colorout=auto', 'optimization=2', 'b_pie=true'])

# inheritance/overloaded.h intentionally hides virtual functions. Headers from
# dependencies are system headers, so this is only an issue for local headers
if meson.get_compiler('cpp').get_argument_syntax() == 'gcc'
  add_project_arguments('-Wno-overloaded-virtual', language: 'cpp')
endif

subdir('semiwrap')

swtest_ft__ft_sources += files(
  'src/swtest/ft/src/fields.cpp',
  'src/swtest/ft/src/ft.cpp',
  'src/swtest/ft/src/using2.cpp',
)

subdir('semiwrap/modules')
